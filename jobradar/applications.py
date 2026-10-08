"""What you have already done about a role.

job-radar finds roles; this remembers what happened next. Without it every
scan re-presents a job you applied to three weeks ago, or one you already
turned down, as though it were news.

The file is yours and is gitignored. Matching is by URL where there is one,
because that is exact, and falls back to company plus a loose title match so
that an entry written by hand still finds its posting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Ordered from earliest to latest. Anything past `applied` means the role has
# had real effort spent on it and should not resurface as a new find.
STATUSES = ["interested", "applied", "submitted", "interviewing",
            "offer", "rejected", "withdrawn", "closed"]

# Statuses that mean "stop showing me this as new".
SETTLED = {"rejected", "withdrawn", "closed"}

SEARCH_PATH = [Path("applications.local.yaml"), Path("applications.yaml")]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def same_employer(org: str, company: str) -> bool:
    """The whole of `company`, as words, inside `org`.

    `Application.matches` accepts the company as a substring of the org, which
    is right for a loose warning and wrong anywhere a decision rests on it: a
    role at "Wise" matches an entry for "Likewise Group". Used where a match
    refuses a draft or writes a record.
    """
    a, b = _norm(org), _norm(company)
    return bool(b) and re.search(rf"\b{re.escape(b)}\b", a) is not None


# Words that change the grade of a job and not which job it is.
_SENIORITY = {"senior", "sr", "snr", "junior", "jr", "principal", "staff", "lead",
              "associate", "i", "ii", "iii", "iv"}


def same_job_title(a: str, b: str) -> bool:
    """Two titles name the same job: equal once normalised, or one inside the
    other as whole words once seniority words are dropped.

    Not the three-shared-words rule `Application.matches` uses for a loose
    warning: "Senior Engineering Manager, Payments" and "... Platform" share
    three words and are two teams, and a rejection from one refused a CV for
    the other, for good. Used where a match refuses a draft.
    """
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ca = " ".join(w for w in na.split() if w not in _SENIORITY)
    cb = " ".join(w for w in nb.split() if w not in _SENIORITY)
    if not ca or not cb:
        return False
    return f" {ca} " in f" {cb} " or f" {cb} " in f" {ca} "


@dataclass
class Application:
    org: str = ""
    role: str = ""
    status: str = "interested"
    date: str | None = None
    note: str = ""
    url: str = ""

    def matches(self, job) -> bool:
        if self.url and job.url:
            a = re.sub(r"[?#].*$", "", self.url.rstrip("/").lower())
            b = re.sub(r"[?#].*$", "", job.url.rstrip("/").lower())
            if a == b:
                return True
        if not self.org:
            return False
        org_a, org_b = _norm(self.org), _norm(job.company)
        # "n8n · remote EMEA" should still match the company "n8n"
        if not (org_b and (org_b in org_a or org_a.startswith(org_b))):
            return False
        if not self.role:
            return True
        ra, rb = _norm(self.role), _norm(job.title)
        return bool(ra and rb and (ra in rb or rb in ra or
                                   len(set(ra.split()) & set(rb.split())) >= 3))


@dataclass
class Tracker:
    apps: list[Application] = field(default_factory=list)

    @classmethod
    def load(cls, path=None) -> "Tracker":
        p = Path(path) if path else next((x for x in SEARCH_PATH if x.exists()), None)
        if not p or not p.exists():
            return cls()
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        rows = raw.get("applications") if isinstance(raw, dict) else raw
        apps = []
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            apps.append(Application(
                org=str(r.get("org") or ""), role=str(r.get("role") or ""),
                status=str(r.get("status") or "interested").lower(),
                date=r.get("date") and str(r.get("date")), note=str(r.get("note") or ""),
                url=str(r.get("url") or "")))
        return cls(apps)

    def find(self, job):
        return next((a for a in self.apps if a.matches(job)), None)

    def annotate(self, jobs: list) -> int:
        """Tag every job that already has a history. Returns how many matched."""
        n = 0
        for j in jobs:
            a = self.find(j)
            if not a:
                continue
            n += 1
            j.app_status = a.status
            label = a.status.replace("_", " ")
            j.flags.append(f"{label}{' on ' + a.date if a.date else ''}"
                           + (f" — {a.note}" if a.note else ""))
        return n
