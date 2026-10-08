"""Bring what is already known into the application record, once.

Before this, "what happened with this role" lived in three places that did not
agree: a status and a note in `role_state`, a YAML file the scan reads, and
markdown handoff files written by hand. This reads the first and the third.
The YAML was already imported into `role_state` by `store.migrate`, so reading
`role_state` covers it.

Nothing here invents a fact. A date is taken from a note only when a word next
to it says what it is the date of ("applied 6 Oct", "rejected 6 Oct"): a bare
"4 Aug" or "deadline 6 Aug" could be anything, and a deadline is not a send
date. A date taken from `updated_at` is marked ESTIMATED, because `updated_at`
is when a status last changed, which is not when the application went in. A
handoff row that matches no role is reported and left out, never created.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from . import applications, store

SENT = ("applied", "submitted", "interviewing", "offer", "rejected", "withdrawn")
_MON = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
_DATE = (rf"(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<m>{_MONTHS})[a-z]*\.?"
         rf"(?:,?\s+(?P<y>\d{{4}}))?")
# The word that says what a date is the date of.
_APPLIED_CUE = re.compile(
    rf"\b(?:applied|submitted|sent(?:\s+in)?|went\s+in)\s*(?:on\s+)?{_DATE}", re.I)
_FINAL_CUE = {
    "rejected": r"(?:reject(?:ed|ion)|declined|unsuccessful)",
    "withdrawn": r"withdr[ae]w[a-z]*",
    "interviewing": r"interview(?:ing|ed)?",
    "offer": r"offer(?:ed)?",
}
_VIA = re.compile(
    r"\bvia\s+(?P<r>[A-Za-z0-9 .&/+-]{2,40}?)"
    rf"(?=\.(?=\s|$)|[,;(]|$|\s+\||\s+ref\b|\s+\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTHS}))", re.I)


@dataclass
class Planned:
    uid: str
    applied_on: str
    route: str
    source: str
    estimated: bool = False
    cv_label: str = ""
    final_status: str = ""
    final_on: str = ""
    # Where final_on came from, which is not always where applied_on did: a
    # note can state the application day and say nothing of the rejection.
    final_source: str = ""


@dataclass
class PlanResult:
    rows: list[Planned] = field(default_factory=list)
    unmatched: list[dict] = field(default_factory=list)
    already_recorded: int = 0           # roles skipped because a record exists


# Statuses whose date can be after the day the status was set: an interview is
# booked before it happens, an offer can name a later date. Their year-less
# dates are the occurrence NEAREST `updated_at`. Everything else (applied,
# rejected, withdrawn) had happened by the time the status was set, so it is
# the most recent occurrence not after it.
_MAY_BE_AHEAD = ("interviewing", "offer")


def _resolve(m: re.Match, ref: date, nearest: bool = False) -> str | None:
    """A matched day and month as an ISO date. With no year, the most recent
    such date that is not after `ref` (the day the status last changed), or
    with `nearest` the occurrence closest to `ref` either side.

    Capping an interview at `ref` filed "Interview 12 Oct", noted on 8
    October, as 12 October of the year before, which then dated the
    application a year early and made the duplicate guard say so."""
    try:
        day, mon = int(m["d"]), _MON[m["m"][:3].lower()]
        if m["y"]:
            return date(int(m["y"]), mon, day).isoformat()
        cands = []
        for y in (ref.year - 1, ref.year, ref.year + 1):
            try:
                cands.append(date(y, mon, day))
            except ValueError:
                continue                       # 29 Feb outside a leap year
        if not cands:
            return None
        if nearest:
            return min(cands, key=lambda c: abs((c - ref).days)).isoformat()
        past = [c for c in cands if c <= ref]
        return max(past).isoformat() if past else None
    except ValueError:
        return None


_ISO_IN_NOTE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_MON_NAMES = {v: k for k, v in _MON.items()}


def _iso_as_words(note: str) -> str:
    """"applied 2026-09-14" as "applied 14 sep 2026", so the cue patterns read
    it. It was not read at all, and the application was dated from
    updated_at instead. A date that does not exist is left as it was."""
    def words(m):
        try:
            d = date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return m[0]
        return f"{d.day} {_MON_NAMES[d.month]} {d.year}"
    return _ISO_IN_NOTE.sub(words, note or "")


def _cued_date(note: str, cue: re.Pattern, ref: date, nearest: bool = False) -> str | None:
    m = cue.search(_iso_as_words(note))
    return _resolve(m, ref, nearest) if m else None


def plan_from_state(con) -> list[Planned]:
    out = []
    rows = con.execute(
        "SELECT s.uid, s.status, s.note, s.updated_at FROM role_state s "
        "WHERE s.status IN (%s) AND NOT EXISTS "
        "(SELECT 1 FROM applications a WHERE a.uid = s.uid) ORDER BY s.uid"
        % ",".join("?" * len(SENT)), SENT).fetchall()
    for r in rows:
        note = r["note"] or ""
        try:
            ref = date.fromisoformat((r["updated_at"] or "")[:10])
        except ValueError:
            # No date to estimate from and none in the note to trust: left
            # out rather than filed under today.
            continue
        via = _VIA.search(note)
        route = via["r"].strip() if via else ""
        stated = _cued_date(note, _APPLIED_CUE, ref)
        final_status = final_on = final_source = ""
        if r["status"] not in ("applied", "submitted"):
            final_status = r["status"]
            final_on = _cued_date(note, re.compile(
                rf"\b{_FINAL_CUE[final_status]}\s*(?:on\s+)?{_DATE}", re.I), ref,
                nearest=final_status in _MAY_BE_AHEAD)
            final_source = "import:note" if final_on else store.ESTIMATED_SOURCE
            final_on = final_on or ref.isoformat()
        if stated:
            p = Planned(uid=r["uid"], applied_on=stated, route=route,
                        source="import:note")
        else:
            # An application cannot be later than its rejection or withdrawal,
            # so the estimate is capped there. Never at an interview or offer
            # date: those may be in the future, and capping at one dated the
            # application by a day that had not happened yet.
            est = ref.isoformat()
            if final_on and final_status not in _MAY_BE_AHEAD:
                est = min(est, final_on)
            p = Planned(uid=r["uid"], applied_on=est, route=route,
                        source=store.ESTIMATED_SOURCE, estimated=True)
        p.final_status, p.final_on, p.final_source = final_status, final_on, final_source
        out.append(p)
    return out


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def plan_from_handoff(con, path: Path):
    """Rows of a `| Role | Company | Route | CV |` table, dated from the file's
    own title ("# Applications, 26 September 2026") or its filename."""
    text = Path(path).read_text(encoding="utf-8")        # raises FileNotFoundError
    title = re.search(r"^#\s.*?(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text, re.M)
    on = None
    if title:
        m = re.fullmatch(rf"{_DATE}", f"{title[1]} {title[2][:3]} {title[3]}", re.I)
        on = _resolve(m, date.today()) if m else None
    if not on:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", Path(path).name)
        on = m[1] if m else None
    if not on:
        raise ValueError(f"{path}: no date in the title or the filename")
    jobs = con.execute("SELECT uid, company, title, url FROM roles").fetchall()
    rows, unmatched, header = [], [], None
    for line in text.splitlines():
        if not line.startswith("|") or set(line) <= set("|-: "):
            continue
        cells = _cells(line)
        low = [c.lower() for c in cells]
        if "role" in low and "company" in low:
            header = low
            continue
        if header is None or len(cells) < len(header):
            continue
        d = dict(zip(header, cells))
        app = applications.Application(org=d.get("company", ""), role=d.get("role", ""))
        hit = [j for j in jobs
               if applications.same_employer(app.org, j["company"])
               and app.matches(SimpleNamespace(url="", company=j["company"], title=j["title"]))]
        if len(hit) == 1:
            rows.append(Planned(uid=hit[0]["uid"], applied_on=on, route=d.get("route", ""),
                                source="import:handoff", cv_label=d.get("cv", "")))
        else:
            unmatched.append({"company": d.get("company", ""), "role": d.get("role", ""),
                              "why": "no role matches" if not hit else f"{len(hit)} roles match"})
    return rows, unmatched


def build_plan(con, handoffs: list[Path]) -> PlanResult:
    """The state-derived rows, improved by any handoff row for the same role.

    A handoff row carries a date somebody wrote down at the time, so it
    replaces an ESTIMATED one. It fills a route only where there is none, and
    never overrides a date or route the role's own note stated.
    """
    result = PlanResult(rows=plan_from_state(con))
    by_uid = {p.uid: p for p in result.rows}
    recorded = {r["uid"] for r in con.execute(
        "SELECT s.uid FROM role_state s WHERE s.status IN (%s) AND EXISTS "
        "(SELECT 1 FROM applications a WHERE a.uid=s.uid)" % ",".join("?" * len(SENT)),
        SENT)}
    for path in handoffs:
        rows, unmatched = plan_from_handoff(con, path)
        result.unmatched += unmatched
        for h in rows:
            if con.execute("SELECT 1 FROM applications WHERE uid=?", (h.uid,)).fetchone():
                recorded.add(h.uid)
                continue
            s = by_uid.get(h.uid)
            if s is None:
                result.rows.append(h)
                by_uid[h.uid] = h
            elif s.source == "import:handoff":
                result.unmatched.append({"company": h.uid, "role": "",
                                         "why": "already planned from an earlier handoff file"})
            else:
                if s.estimated:
                    s.applied_on, s.estimated, s.source = h.applied_on, False, "import:handoff"
                s.route = s.route or h.route
                s.cv_label = h.cv_label
    result.already_recorded = len(recorded)
    return result


def apply_plan(con, plan: list[Planned]) -> dict:
    """Write the plan in one transaction: all of it or none of it."""
    own = not con.in_transaction
    if own:
        con.execute("BEGIN IMMEDIATE")
    written = skipped = 0
    try:
        for p in plan:
            if store.record_application(con, p.uid, applied_on=p.applied_on,
                                        route=p.route, source=p.source):
                written += 1
                if p.cv_label:
                    store.add_event(con, p.uid, "note", at=p.applied_on,
                                    detail=f"CV sent: {p.cv_label}", source=p.source)
            else:
                skipped += 1
            if p.final_status:
                store.add_event(con, p.uid, p.final_status, at=p.final_on,
                                detail="imported from note",
                                source=p.final_source or p.source)
        if own:
            con.execute("COMMIT")
    except BaseException:
        if own:
            con.execute("ROLLBACK")
        raise
    return {"written": written, "skipped": skipped}
