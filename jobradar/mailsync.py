"""Read hiring mail into proposals that wait for a yes.

`classify` and `match` only read and decide. `propose` writes proposals into
`mail_proposals` and nothing else; `apply_proposals` is the only thing here
that changes a role's status or history, and only for the ids it is given.

A message is untrusted text from outside. It is matched against fixed phrase
lists and nothing in it is followed, fetched or executed: there is no code path
here that treats text as an instruction, and no network, process or mail
library is imported. A match that is not clear is `ambiguous` or `other`, and
those produce no proposal, because a wrong "rejected" moves a live application
off the board.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date

from . import store

# Phrases, not a model. Each family is a list of regexes matched against the
# lower-cased subject and the first BODY_CHARS of the body.
# Every rejection phrase names the decision itself. "unfortunately,? we" used
# to be one, and it is how acknowledgements say they cannot reply to everyone
# or give feedback, and how a recruiter moves a call: each came back a
# rejection and `apply --all` took a live application off the board. Those
# words are now `_HEDGE`, which can make a message ambiguous but never decides.
_REJECT = [r"decided to move forward with (?:other )?candidates",
           r"decided not to (?:move|proceed|progress)",
           r"not (?:be )?(?:moving|progressing|proceeding) (?:forward )?with your",
           r"(?:cannot|can ?not|can't|unable to|not able to) (?:progress\b|proceed with your"
           r"|move forward with your|offer you (?:a|the|this) (?:position|role|job))",
           r"we will not be (?:taking|progressing|moving) (?:forward )?your application",
           r"(?:have|has) not been successful", r"application (?:was|has been) unsuccessful",
           r"position has been filled", r"unsuccessful on this occasion",
           r"regret to inform"]
# Polite words that rejections use and so do acknowledgements, reschedules and
# feedback disclaimers. Alone, or beside an acknowledgement, they leave the
# message `ambiguous`: a person reads it, nothing is proposed.
_HEDGE = [r"\bunfortunately\b", r"\bregret\b"]
# The application is still alive. Beside rejection wording ("the Head of
# Platform position has been filled, but your application for Engineering
# Manager remains under review") the message says two things and is ambiguous.
_STILL_LIVE = [r"(?:remains|is still|still) (?:under review|being considered|in consideration)"]
_INTERVIEW = [r"confirmed interview", r"interview schedule",
              r"invite you to (?:an? )?interview",
              # Invited to what: "pleased to invite you" alone also opens every
              # careers evening and webinar invitation.
              r"pleased to invite you to (?:an? |the |your )?(?:\w+ )?"
              r"(?:interview|stage|round|call|conversation|meeting|chat|assessment)",
              r"your interview with",
              r"interview (?:is )?(?:scheduled|booked|confirmed)",
              r"book (?:a|your) (?:time|slot|call)",
              r"availability for an? (?:interview|call)",
              r"(?:love|like|happy|keen) to interview you"]
_OFFER = [r"pleased to offer you", r"offer of employment",
          r"we(?:'d| would) like to offer you"]
_ACK = [r"you already applied",
        r"application (?:was )?(?:sent|received|submitted)",
        r"thank(?:s| you) for (?:applying|your application|your interest)",
        r"still (?:in the process of )?compiling a shortlist",
        r"we(?:'ve| have) received your application", *_STILL_LIVE]
# Things that look like hiring mail and are not a change of status. Checked on
# the subject first; the body alone only decides when nothing else matched, so
# an invitation that offers to reschedule is still an invitation.
_NOT_STATUS_SUBJECT = [r"^\s*(?:canceled|cancelled):", r"out of office", r"automatic reply",
                       r"\breschedul"]
_NOT_STATUS_BODY = [r"reschedul", r"out of office", r"automatic reply"]

BODY_CHARS = 3000
MATCH_BODY_CHARS = 1500
EVIDENCE_CHARS = 200

_FAMILIES = (("rejection", _REJECT), ("interview", _INTERVIEW),
             ("offer", _OFFER), ("acknowledgement", _ACK))
_COMPILED = {k: [re.compile(p, re.I) for p in pats] for k, pats in _FAMILIES}
_NS_SUBJECT = [re.compile(p, re.I) for p in _NOT_STATUS_SUBJECT]
_NS_BODY = [re.compile(p, re.I) for p in _NOT_STATUS_BODY]
_HEDGE_RX = [re.compile(p, re.I) for p in _HEDGE]
# The stage an interview invite names, kept on the event so two rounds read
# as two rounds.
_STAGE = re.compile(r"\b(?:(?:first|second|third|fourth|final|next|1st|2nd|3rd|4th)\s+"
                    r"(?:round|stage|interview)|(?:technical|hiring manager|panel|onsite|"
                    r"on-site|culture|case study)\s+(?:interview|round|stage))\b", re.I)
_LIVE_RX = [re.compile(p, re.I) for p in _STILL_LIVE]


@dataclass
class Verdict:
    kind: str                      # rejection|interview|offer|acknowledgement|ambiguous|other
    evidence: str = ""
    detail: str = ""


@dataclass
class Match:
    uid: str | None
    why: str = ""
    candidates: list[str] = field(default_factory=list)


def _sentence(text: str, start: int) -> str:
    """The sentence around a match, cut to EVIDENCE_CHARS."""
    left = max(text.rfind(c, 0, start) for c in ".!?\n")
    ends = [i for i in (text.find(c, start) for c in ".!?\n") if i != -1]
    right = min(ends) if ends else len(text)
    return re.sub(r"\s+", " ", text[left + 1: right + 1]).strip()[:EVIDENCE_CHARS]


def _first_hit(text: str, patterns) -> tuple[int, int] | None:
    best = None
    for rx in patterns:
        m = rx.search(text)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), m.end())
    return best


def classify(msg: dict) -> Verdict:
    subject = str(msg.get("subject") or "")
    body = str(msg.get("body") or "")[:BODY_CHARS]
    text = f"{subject}\n{body}"
    if any(rx.search(subject) for rx in _NS_SUBJECT):
        why = "reschedule: not a status change" if re.search(r"reschedul", subject, re.I) else ""
        return Verdict("other", _sentence(text, 0), why)
    hits = {k: _first_hit(text, rxs) for k, rxs in _COMPILED.items()}
    found = {k: h for k, h in hits.items() if h}
    hedge = _first_hit(text, _HEDGE_RX)
    if not found:
        if hedge:
            return Verdict("ambiguous", _sentence(text, hedge[0]),
                           "unfortunately/regret with no rejection phrase")
        why = ""
        if any(rx.search(text) for rx in _NS_BODY):
            why = "reschedule: not a status change"
        return Verdict("other", "", why)

    def verdict(kind: str) -> Verdict:
        start, _ = found[kind]
        detail = ""
        if kind == "acknowledgement" and re.search(r"you already applied", text, re.I):
            detail = "duplicate application refused by the employer"
        if kind == "interview":
            stage = _STAGE.search(text)
            detail = stage.group(0).lower() if stage else ""
        return Verdict(kind, _sentence(text, start), detail)

    r, i, o = "rejection" in found, "interview" in found, "offer" in found
    if r and (i or o):
        # Both said in one message: never decided on a guess.
        start = min(found[k][0] for k in ("rejection", "interview", "offer") if k in found)
        return Verdict("ambiguous", _sentence(text, start),
                       "rejection and interview/offer wording in one message")
    live = _first_hit(text, _LIVE_RX)
    if r and live:
        return Verdict("ambiguous", _sentence(text, min(found["rejection"][0], live[0])),
                       "rejection wording, and an application still under review")
    if o:
        return verdict("offer")
    if i:
        return verdict("interview")
    if r:
        return verdict("rejection")
    if hedge:
        # "Thanks for applying ... unfortunately we cannot reply to everyone":
        # an acknowledgement in rejection's vocabulary. Not decided either way.
        return Verdict("ambiguous", _sentence(text, hedge[0]),
                       "acknowledgement wording with unfortunately/regret")
    return verdict("acknowledgement")


# ----------------------------------------------------------------- matching

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


_TWO_PART_SUFFIXES = {"co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "co.nz", "co.jp", "com.br"}


def _domain_label(sender: str) -> str:
    """"thornfieldsystems" from "pcutler@thornfieldsystems.net", "garnethealth" from
    "careers@garnethealth.co.uk"."""
    m = re.search(r"@([A-Za-z0-9.-]+)", sender or "")
    if not m:
        return ""
    parts = m.group(1).lower().strip(".").split(".")
    if len(parts) < 2:
        return ""
    if ".".join(parts[-2:]) in _TWO_PART_SUFFIXES and len(parts) >= 3:
        return parts[-3]
    return parts[-2]


def _has_phrase(haystack_norm: str, phrase_norm: str) -> bool:
    return bool(phrase_norm) and f" {phrase_norm} " in f" {haystack_norm} "


# Employers named with ordinary words. Sources include Next, HERE, Today and
# Link, and "Next steps on your application" attributed Acme's rejection to
# Next. A name like these, or any one-word name of six letters or fewer,
# counts only where it is written as a name (capitalised, not inside one of
# the phrases below, and for these words not merely capitalised by starting a
# sentence) or is the sender's domain. Otherwise it is only a candidate.
_COMMON_WORDS = {"next", "here", "today", "link", "now", "open", "apply", "indeed",
                 "team", "people", "work", "jobs", "careers", "talent", "update",
                 "hello", "thanks", "please", "news"}
_NOT_A_NAME = re.compile(
    r"\b(?:next\s+(?:steps?|stages?|rounds?|week|month)|here\s+(?:is|are|you)|here's|"
    r"click\s+here|link\s+(?:to|below|above|in)|today\s*,|today\s+we|apply\s+now|"
    r"open\s+(?:roles?|positions?|vacanc\w*))\b", re.I)


def _weak_name(company: str) -> bool:
    return company in _COMMON_WORDS or (" " not in company and len(company) <= 6)


def _sentence_start(text: str, at: int) -> bool:
    before = text[:at].rstrip(" \t\"'([")
    return not before or before[-1] in ".!?:\n"


def _written_as_name(raw: str, company: str) -> bool:
    """`company` (normalised) appears in `raw` where it reads as a name."""
    rx = re.compile(r"(?<![A-Za-z0-9])" + r"[^A-Za-z0-9]+".join(map(re.escape, company.split()))
                    + r"(?![A-Za-z0-9])", re.I)
    phrases = [m.span() for m in _NOT_A_NAME.finditer(raw)]
    for m in rx.finditer(raw):
        if not m.group(0)[0].isupper():
            continue
        if any(a < m.end() and m.start() < b for a, b in phrases):
            continue
        if company in _COMMON_WORDS and _sentence_start(raw, m.start()):
            continue
        return True
    return False


def match(msg: dict, roles: list[dict]) -> Match:
    """Which role a message is about. Never a guess: when two roles fit, the
    answer is None and the candidates are named."""
    raw_subject = str(msg.get("subject") or "")
    raw = f"{raw_subject}\n{str(msg.get('body') or '')[:MATCH_BODY_CHARS]}"
    subject = _norm(raw_subject)
    body = _norm(str(msg.get("body") or "")[:MATCH_BODY_CHARS])
    both = f"{subject} {body}"
    label = _domain_label(str(msg.get("sender") or ""))
    by_company: dict[str, list[dict]] = {}
    for r in roles:
        by_company.setdefault(_norm(r["company"]), []).append(r)
    named, by_domain, weak_only = [], [], []
    for company in by_company:
        if not company:
            continue
        if label and label == company.replace(" ", ""):
            named.append(company)
            by_domain.append(company)
        elif _has_phrase(both, company):
            if _weak_name(company) and not _written_as_name(raw, company):
                weak_only.append(company)
            else:
                named.append(company)
    if not named:
        if weak_only:
            return Match(None, "ambiguous company: only ordinary words that are also "
                               "employer names", [r["uid"] for c in weak_only
                                                   for r in by_company[c]])
        return Match(None, "no company in the message matches a role", [])
    if len(named) > 1:
        # The sender's own domain is the employer speaking, so it outranks a
        # name in the subject, which can be any company the message mentions.
        in_subject = [c for c in named if _has_phrase(subject, c)]
        if len(by_domain) == 1:
            named = by_domain
        elif len(in_subject) == 1:
            named = in_subject
        else:
            return Match(None, "the message names more than one company",
                         [r["uid"] for c in named for r in by_company[c]])
    mine = by_company[named[0]]
    hits = []
    for r in mine:
        t = _norm(r["title"])
        if t and _has_phrase(both, t):
            hits.append((len(t), r))
    if not hits:
        if len(mine) == 1:
            return Match(mine[0]["uid"], "the only role at that company")
        return Match(None, "the message names no title and the company has several roles",
                     [r["uid"] for r in mine])
    top = max(n for n, _ in hits)
    best = [r for n, r in hits if n == top]
    if len(best) > 1:
        applied = [r for r in best if r.get("status") not in (None, "", "new", "interested",
                                                              "skipped", "closed")]
        if len(applied) == 1:
            return Match(applied[0]["uid"], "the title matches several roles; this is the one applied for")
        return Match(None, "the title matches several roles", [r["uid"] for r in best])
    return Match(best[0]["uid"], "company and title are named in the message")


# ------------------------------------------------------------------ propose

# The folders a read must cover. Rejections are filed in Deleted Items and
# Junk by mail rules as often as they land in the Inbox, and a search query
# skips Deleted Items entirely: four rejections sat unseen for exactly that
# reason. A read of fewer folders is reported as partial, never as complete.
REQUIRED_FOLDERS = ("inbox", "deleteditems", "junkemail")
_FOLDER_NAMES = {"deleteditems": "Deleted Items", "junkemail": "Junk Email", "inbox": "Inbox"}

_STATUS_FOR = {"rejection": "rejected", "interview": "interviewing", "offer": "offer",
               "acknowledgement": ""}
_EVENT_FOR = {"rejection": "rejected", "interview": "interviewing", "offer": "offer",
              "acknowledgement": "acknowledged"}
# Statuses that mean "an application is on file".
_APPLIED_LIKE = ("applied", "submitted", "interviewing", "offer", "rejected", "withdrawn")
# Statuses an acknowledgement may move forward to applied.
_NOT_YET_APPLIED = ("new", "interested", "skipped")


def _folder_key(name: str) -> str:
    return re.sub(r"[^a-z]", "", str(name).lower())


def _day(received: str) -> str:
    try:
        return date.fromisoformat(str(received)[:10]).isoformat()
    except ValueError:
        return ""


def _validate(payload) -> tuple[list[str], list[dict]]:
    if not isinstance(payload, dict):
        raise ValueError("the mail file must be a JSON object with folders_read and messages")
    folders, msgs = payload.get("folders_read"), payload.get("messages")
    if not isinstance(folders, list) or not isinstance(msgs, list):
        raise ValueError("the mail file needs a `folders_read` list and a `messages` list")
    for n, m in enumerate(msgs, 1):
        if not isinstance(m, dict) or not str(m.get("id") or "").strip():
            raise ValueError(f"message {n} has no `id`; an unidentifiable message cannot be "
                             f"told apart from one already proposed")
    return [_folder_key(f) for f in folders], msgs


def _warnings(con, uid: str, status: str, kind: str, folder: str, day: str) -> list[str]:
    out = []
    folder = _folder_key(folder or "")
    if folder in ("deleteditems", "junkemail"):
        out.append(f"found in {_FOLDER_NAMES[folder]}")
    if ((kind in ("rejection", "interview", "offer") and status not in _APPLIED_LIKE)
            or (kind == "acknowledgement" and not store.applications_for(con, uid))):
        out.append("role is not recorded as applied")
    on, estimated = store.applied_on_estimate(con, uid)
    if on and day and day < on:
        out.append("message predates the application"
                   + (" (whose date is estimated)" if estimated else ""))
    if not day:
        out.append("message has no readable date: today will be used")
    if kind == "offer":
        out.append("an offer is your decision: check before applying")
    if store.is_backwards(status, _STATUS_FOR[kind]):
        out.append(f"role is already at {status}: this would move it backwards")
    if kind == "rejection" and status in ("interviewing", "offer"):
        out.append(f"role is {status}: check this rejection is for the same process")
    return out


def propose(con, payload: dict, *, allow_partial: bool = False) -> dict:
    """Turn a read of the mailbox into pending proposals. Writes only
    `mail_proposals`; role status and history are untouched.

    `allow_partial` does not change what is written. It is carried in the
    summary so the caller reports a partial read as partial either way: the
    folders that were read still produced real signal and are kept.
    """
    folders, msgs = _validate(payload)
    missing = [f for f in REQUIRED_FOLDERS if f not in folders]
    summary = {"read": len(msgs), "folders": folders, "missing": missing,
               "proposals": 0, "unmatched": 0, "other": 0, "already_proposed": 0,
               "already_current": 0, "other_messages": [],
               "partial": bool(missing) or not msgs, "allow_partial": allow_partial}
    roles = [dict(r) for r in con.execute(
        "SELECT r.uid, r.company, r.title, COALESCE(s.status,'new') AS status "
        "FROM roles r LEFT JOIN role_state s ON s.uid=r.uid")]
    status_of = {r["uid"]: r["status"] for r in roles}
    for m in msgs:
        mid = str(m["id"])
        if con.execute("SELECT 1 FROM mail_proposals WHERE message_id=?", (mid,)).fetchone():
            summary["already_proposed"] += 1
            continue
        v = classify(m)
        if v.kind in ("other", "ambiguous"):
            summary["other"] += 1
            summary["other_messages"].append(
                {"id": mid, "subject": str(m.get("subject") or ""), "kind": v.kind,
                 "detail": v.detail})
            continue
        hit = match(m, roles)
        day = _day(m.get("received") or "")
        row = dict(message_id=mid, folder=str(m.get("folder") or ""),
                   received=str(m.get("received") or ""), subject=str(m.get("subject") or "")[:300],
                   kind=v.kind, new_status=_STATUS_FOR[v.kind], event_kind=_EVENT_FOR[v.kind],
                   happens_at=day, evidence=v.evidence)
        if hit.uid is None:
            # Kept, not dropped: a rejection nobody can attribute to a role is
            # exactly the one a person needs to see.
            warns = [f"no role matched: {hit.why}"]
            if hit.candidates:
                warns.append("candidates: " + ", ".join(hit.candidates))
            row.update(uid="", warnings=json.dumps(warns))
            summary["unmatched"] += 1
        else:
            status = status_of.get(hit.uid, "new")
            if v.kind == "interview" and status == "interviewing":
                # A further round. Skipped as already current, it left no
                # event, so the second interview was never on file. Proposed
                # as an event only when it is later than the last one; the
                # status stays where it is.
                last = con.execute("SELECT MAX(at) FROM app_events WHERE uid=? "
                                   "AND kind='interviewing'", (hit.uid,)).fetchone()[0]
                if day and (not last or day > last):
                    row["new_status"] = ""
                    if v.detail:
                        row["evidence"] = f"{v.detail}: {v.evidence}"[:EVIDENCE_CHARS]
                else:
                    summary["already_current"] += 1
                    continue
            elif v.kind != "acknowledgement" and status == row["new_status"]:
                summary["already_current"] += 1
                continue
            if v.kind == "acknowledgement" and con.execute(
                    "SELECT 1 FROM app_events WHERE uid=? AND kind='acknowledged'",
                    (hit.uid,)).fetchone():
                summary["already_current"] += 1
                continue
            if (v.kind == "acknowledgement" and not store.applications_for(con, hit.uid)
                    and status in _NOT_YET_APPLIED):
                # An employer saying an application exists, about a role
                # nothing records as applied for: the Brightwell duplicate. The
                # proposal records the application and moves the status on,
                # and its warning means it waits for an explicit id.
                row["new_status"] = "applied"
            row.update(uid=hit.uid,
                       warnings=json.dumps(_warnings(con, hit.uid, status, v.kind,
                                                     str(m.get("folder") or ""), day)))
            summary["proposals"] += 1
        con.execute(
            "INSERT OR IGNORE INTO mail_proposals (message_id, folder, received, subject, kind, "
            "uid, new_status, event_kind, happens_at, evidence, warnings, state, created_at) "
            "VALUES (:message_id,:folder,:received,:subject,:kind,:uid,:new_status,:event_kind,"
            ":happens_at,:evidence,:warnings,'pending',:created_at)",
            {**row, "created_at": store._now()})
    return summary


def list_proposals(con, *, state: str | None = "pending") -> list[dict]:
    q = ("SELECT p.*, r.company, r.title, COALESCE(s.status,'new') AS current_status "
         "FROM mail_proposals p LEFT JOIN roles r ON r.uid=p.uid "
         "LEFT JOIN role_state s ON s.uid=p.uid")
    args: tuple = ()
    if state:
        q += " WHERE p.state=?"
        args = (state,)
    out = []
    for r in con.execute(q + " ORDER BY p.id", args):
        d = dict(r)
        d["warnings"] = json.loads(d["warnings"] or "[]")
        out.append(d)
    return out


def _already(con, r: dict) -> bool:
    """The role is already at the status this proposal would set. Two messages
    about one rejection make two proposals; the second has nothing to do."""
    return bool(r["uid"] and r["new_status"] and r["kind"] != "acknowledgement"
                and store.status_of(con, r["uid"]) == r["new_status"])


def apply_proposals(con, ids: list[int], *, all_clean: bool = False,
                    role_uid: str | None = None) -> dict:
    """Apply pending proposals: the ids named, or with `all_clean` every pending
    proposal that has a role and no warnings. The rest of a `--all` are listed
    as needing an explicit id, never applied on the strength of "all".

    `role_uid` names the role. It overrides the role a proposal matched: a
    person naming a role has said which one, and applying to a different one
    while printing "applied" is the wrong rejection on a live application. The
    override is reported in `retargeted`. It cannot be combined with
    `all_clean`, which would point every pending proposal at one role.

    Proposals apply in the order the messages were received, not the order
    they were proposed: a mailbox read newest first gives a rejection a lower
    id than the interview invite before it. Warnings are worked out again at
    apply time against the status the role has then, including after earlier
    proposals in this same run: the stored ones describe the role when the
    proposal was made. `--all` takes only proposals with no warning now, so it
    never moves a status backwards; an explicit id may, and the warning comes
    back in `warned` for the caller to print.

    One transaction: all of it or none of it.
    """
    if all_clean and role_uid:
        raise ValueError("--role names one role and --all applies many: name the ids instead")
    out = {"applied": [], "skipped": [], "needs_explicit": [], "warned": [], "retargeted": []}
    if all_clean:
        todo = list_proposals(con)
    else:
        todo = []
        for i in ids:
            r = con.execute("SELECT * FROM mail_proposals WHERE id=?", (i,)).fetchone()
            if r is None or r["state"] != "pending":
                state = "no such proposal" if r is None else r["state"]
                out["skipped"].append((i, f"not pending ({state})"))
                continue
            d = dict(r)
            d["warnings"] = json.loads(d["warnings"] or "[]")
            todo.append(d)
    # Received order. A proposal with no readable date sorts last: it will be
    # dated today, so it is the newest thing that can be said about the role.
    todo.sort(key=lambda r: (r["happens_at"] or "9999-99-99", r["id"]))
    own = not con.in_transaction
    if own:
        con.execute("BEGIN IMMEDIATE")
    try:
        for r in todo:
            uid = role_uid or r["uid"]
            if not uid:
                if all_clean:
                    out["needs_explicit"].append(r["id"])
                else:
                    out["skipped"].append((r["id"], "no role matched; pass --role to say which"))
                continue
            if not con.execute("SELECT 1 FROM roles WHERE uid=?", (uid,)).fetchone():
                out["skipped"].append((r["id"], f"role {uid} no longer exists"))
                continue
            if _already(con, {**r, "uid": uid}):
                if all_clean:
                    out["needs_explicit"].append(r["id"])
                else:
                    out["skipped"].append((r["id"], f"role is already {r['new_status']}; "
                                           f"dismiss the proposal instead"))
                continue
            now = _warnings(con, uid, store.status_of(con, uid), r["kind"], r["folder"],
                            r["happens_at"])
            if all_clean and now:
                out["needs_explicit"].append(r["id"])
                continue
            for w in now:
                out["warned"].append((r["id"], w))
            if r["uid"] and r["uid"] != uid:
                out["retargeted"].append((r["id"], r["uid"], uid))
            source = f"mail:{r['message_id']}"
            at = r["happens_at"] or None
            # The event first, with the message's own words as its detail:
            # `transition` writes the same event with an empty detail, and the
            # unique index then drops that duplicate.
            if r["kind"] == "acknowledgement" and not store.applications_for(con, uid):
                # The employer says an application exists and nothing here
                # records one. Filed, so the duplicate guard can see it, under
                # a source that marks its date as "on or before".
                store.record_application(con, uid, applied_on=at,
                                         source=f"{store.ACK_SOURCE}{r['message_id']}")
            store.add_event(con, uid, r["event_kind"], at=at,
                            detail=(r["evidence"] or "")[:200], source=source)
            new_status = r["new_status"]
            if (r["kind"] == "acknowledgement"
                    and store.status_of(con, uid) not in _NOT_YET_APPLIED):
                # Only ever forward from new: the role may have moved on since
                # the proposal, or --role may have named another one.
                new_status = ""
            if new_status:
                store.transition(con, uid, new_status, None, source=source, at=at)
            con.execute("UPDATE mail_proposals SET state='applied' WHERE id=?", (r["id"],))
            out["applied"].append(r["id"])
        if own:
            con.execute("COMMIT")
    except BaseException:
        if own:
            con.execute("ROLLBACK")
        raise
    return out


def dismiss(con, ids: list[int]) -> dict:
    out = {"dismissed": [], "skipped": []}
    for i in ids:
        cur = con.execute("UPDATE mail_proposals SET state='dismissed' "
                          "WHERE id=? AND state='pending'", (i,))
        (out["dismissed"] if cur.rowcount else out["skipped"]).append(i)
    return out
