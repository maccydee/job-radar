"""An interview pack built from what was actually sent, not from memory.

Two interviews in one week were prepared from scratch each time, from a CV
that might not have been the one the employer read. This starts from the
record: the CV that went in, the figure given on the form, the dates, and the
places the CV does not answer what the posting asks for.

Deterministic. No network and no model, so it is free to run and a stale pack
cannot look as good as a fresh one. The research half (the company, the
interviewers) needs the web and judgement and lives in
skills/interview-prep/SKILL.md, which has Claude read this pack first.
"""

from __future__ import annotations

import re
from datetime import date

from . import atscheck, store

NO_CV = ("no CV is recorded for this application: find the file you actually "
         "sent before the interview")
_REQUIREMENT = re.compile(r"\b(experience|must|required|you have|essential)\b", re.I)

_RECRUITER = ["Why this role, and why now? (motivation)",
              "What are your salary expectations, and what did you put on the form?",
              "What is your notice period, and when could you start?",
              "Where are you based, and what are your location or travel limits?",
              "Do you have the right to work here, and do you need sponsorship?",
              "Walk me through your CV in two minutes."]
_MANAGER = ["What was the scope of the biggest team and budget you have run? (scope)",
            "Describe the team you would inherit and what you would change first. (team)",
            "Tell me about a trade-off you made between speed and quality, and what it cost.",
            "Tell me about something that failed on your watch. What did you change after?",
            "How do you decide what not to do?",
            "How would you handle a manager who is underperforming?"]
_TECHNICAL = ["Pick the system you know best and draw it. Where would it break first?",
              "What is the hardest technical decision you made recently, and who disagreed?",
              "How do you stay close enough to the work to judge it, without doing it for them?"]
_GENERAL = ["Why this role, and why now?",
            "What is the strongest evidence on your CV for what this posting asks?",
            "What would you ask them?"]


def _posting(con, uid: str) -> tuple[str, str]:
    """(text, where it came from). The stored snapshot is what `generate` read;
    the scan's description is the fallback; neither is an empty string and a
    statement that there is none."""
    for a in store.artifacts_for(con, uid):
        if a["kind"] == "jd_snapshot" and (a.get("body") or "").strip():
            return a["body"], "the posting as saved when a document was drafted"
    row = con.execute("SELECT description FROM roles WHERE uid=?", (uid,)).fetchone()
    if row and (row["description"] or "").strip():
        return row["description"], "the description the scan stored"
    return "", ""


def _newest(con, uid: str, kind: str):
    for a in store.artifacts_for(con, uid):          # newest first
        if a["kind"] == kind:
            return a
    return None


def _requirement_lines(jd: str) -> list[str]:
    out = []
    for raw in re.split(r"[\n]|(?<=[.!?])\s+", jd):
        line = raw.strip(" \t-*•")
        if 15 <= len(line) <= 300 and _REQUIREMENT.search(line):
            out.append(line)
    return out


def _thin(cv: str, jd: str) -> list[tuple[str, list[str]]]:
    out = []
    for line in _requirement_lines(jd):
        missing = [t for t in atscheck.jd_terms(line, limit=4)
                   if not atscheck._present(t, cv)]
        if missing:
            out.append((line, missing))
    return out


def _stage_questions(stage: str, jd: str) -> list[str]:
    s = stage.lower()
    if any(w in s for w in ("recruiter", "screen", "hr", "talent")):
        return _RECRUITER
    if any(w in s for w in ("manager", "hiring", "director", "vp")):
        return _MANAGER
    if any(w in s for w in ("technical", "coding", "system", "architecture", "panel")):
        known = re.search(r"coding (?:round|test|exercise|challenge)|live coding|take[- ]home", jd, re.I)
        bar = (f"The posting mentions a coding bar: \"{known.group(0)}\". Ask what form it "
               f"takes and how it is marked." if known else
               "Ask first: no coding bar is stated in the posting. Ask whether there is "
               "one before the interview, not in it.")
        return [bar, *_TECHNICAL]
    return [f"Stage not recognised ({stage!r}): general questions only. Recognised "
            f"stages are recruiter screen, hiring manager and technical.", *_GENERAL]


def build_pack(con, uid: str, stage: str, today: date) -> str:
    row = con.execute("SELECT company, title, location, salary_label, closes_on "
                      "FROM roles WHERE uid=?", (uid,)).fetchone()
    if row is None:
        raise ValueError(f"no role with id {uid}")
    apps = store.applications_for(con, uid)
    app = apps[0] if apps else None
    events = store.events_for(con, uid)
    jd, jd_from = _posting(con, uid)
    cv_art = _newest(con, uid, "cv")
    cv_text = (cv_art or {}).get("body") or ""
    L: list[str] = [f"# Interview prep: {row['company']}, {row['title']} ({stage})", "",
                    f"Built {today.isoformat()} from the application record. Nothing here "
                    f"came from the web or a model."]

    L += ["", "## What they will have read", ""]
    if app and app["cv_path"]:
        L.append(f"- CV: {app['cv_path']} (recorded on the application)")
    elif cv_art and cv_art["path"]:
        L.append(f"- CV: {cv_art['path']} (the newest CV drafted by generate; not recorded "
                 f"as sent, so confirm it is the one that went in)")
    else:
        L.append(f"- {NO_CV}")
    if app and app["cover_path"]:
        L.append(f"- Cover letter: {app['cover_path']} (recorded on the application)")

    L += ["", "## What you told them", ""]
    L.append(f"- Salary figure given: {app['salary_answer'] if app and app['salary_answer'] else 'not recorded'}")
    L.append(f"- Reference: {app['reference'] if app and app['reference'] else 'not recorded'}")
    L.append(f"- Route: {app['route'] if app and app['route'] else 'not recorded'}")
    on, estimated = store.applied_on_estimate(con, uid)
    if app:
        on, estimated = app["applied_on"], app["source"]
    L.append(f"- Applied on: {store.date_said(on, estimated) if on else 'not recorded'}")

    L += ["", "## The role", "",
          f"- {row['title']} at {row['company']}"
          + (f", {row['location']}" if row["location"] else ""),
          f"- Pay: {row['salary_label'] or 'not stated'}"]
    if row["closes_on"]:
        L.append(f"- Closes: {row['closes_on']}")
    if jd.strip():
        L += [f"- Posting text: {jd_from}", "", "> " + re.sub(r"\s+", " ", jd.strip())[:700]
              + (" ..." if len(jd.strip()) > 700 else "")]
    else:
        L.append("- no posting text stored for this role")

    L += ["", "## Timeline", ""]
    dated = []
    # The application's own line only when no `applied` event says the same
    # thing on that day, so a normal record is not listed twice.
    if app and not any(e["kind"] == "applied" and e["at"] == app["applied_on"] for e in events):
        dated.append((app["applied_on"], "applied", f"{app['route']} {app['reference']}".strip(),
                      app["source"]))
    dated += [(e["at"], e["kind"], e["detail"], e["source"]) for e in events]
    for at, kind, detail, source in sorted(dated, key=lambda t: t[0]):
        L.append(f"- {at}  {kind}" + (f"  {detail}" if detail else "") + f"  [{source}]")
    if not dated:
        L.append("- nothing recorded")

    L += ["", "## Where the CV is thin against the posting", "",
          "(keyword check only; read the posting yourself)", ""]
    if not jd.strip():
        L.append("UNMEASURED: no posting text is stored, so nothing to compare with.")
    elif not cv_text.strip():
        L.append("UNMEASURED: no CV text is stored, so nothing to compare with.")
    else:
        thin = _thin(cv_text, jd)
        if not thin:
            L.append("Nothing to flag: no requirement line in the posting names a term the CV lacks.")
        for line, missing in thin:
            L.append(f"- \"{line[:110]}\" -> not in the CV: {', '.join(missing)}")
        if thin:
            L += ["", "Where a term is genuinely missing, prepare an honest bridge from "
                  "what you have done. Do not claim it."]

    L += ["", "## Questions to prepare for this stage", ""]
    L += [f"- {q}" for q in _stage_questions(stage, jd)]

    L += ["", "## Evidence on file", ""]
    ev = store.evidence_for(con, row["company"])
    if ev:
        L += [f"- {store.describe_evidence(e, today)}" for e in ev]
    else:
        L.append("- no salary evidence on file: `job-radar evidence add` records a "
                 "figure with its source")
    return "\n".join(L) + "\n"
