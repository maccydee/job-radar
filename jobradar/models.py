"""Core data types. Every adapter normalises into `Job`."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Salary:
    """A pay figure attached to a posting.

    `confirmed` is the important field. Most postings state no salary at all,
    so `confirmed=False` means "the employer did not publish one", not
    "we failed to parse it". Those roles are shown to the user, labelled.
    Only a confirmed figure can disqualify a role.
    """

    min: float | None = None
    max: float | None = None
    currency: str | None = None
    period: str = "year"  # year | day | hour
    raw: str | None = None
    confirmed: bool = False

    @property
    def top(self) -> float | None:
        """The figure to compare against a floor.

        Uses the top of the band: a posting advertised at 100k-150k clears a
        120k floor, because that is a role you would still talk to them about.
        """
        return self.max if self.max is not None else self.min

    def annualised(self, working_days: int = 220, hours_per_day: int = 8) -> float | None:
        """Day and hour rates converted to an annual figure so a single floor works.

        Without this a 600/day contract reads as 600 and gets binned by any
        sane annual floor.
        """
        t = self.top
        if t is None:
            return None
        if self.period == "day":
            return t * working_days
        if self.period == "hour":
            return t * working_days * hours_per_day
        return t

    SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€"}

    def label(self) -> str:
        """What to show the reader.

        Built from the numbers rather than from whatever string the platform
        supplied, because those strings are often just a heading: Greenhouse
        returns things like "Annual Salary:" and "Local Pay Range" in the same
        field as the figures, and showing that tells the reader nothing.
        """
        if not self.confirmed:
            return "unconfirmed salary"

        sym = self.SYMBOLS.get((self.currency or "").upper(), "")
        cur = "" if sym else (self.currency + " " if self.currency else "")
        per = {"day": "/day", "hour": "/hr"}.get(self.period, "")

        def fmt(v: float) -> str:
            # Millions get an M. Thousands-of-thousands was fine while the
            # only floors allowed were pounds, dollars and euros, where a
            # salary rarely passes 1,000k. Now that a floor can be in rupees,
            # yen or won, a perfectly ordinary salary renders as "INR 4,000k",
            # which the reader has to stop and multiply out, and which reads
            # at a glance like four thousand.
            if self.period == "year" and v >= 1_000_000:
                return f"{sym}{cur}{v / 1_000_000:,.1f}M".replace(".0M", "M")
            if self.period == "year" and v >= 10_000:
                return f"{sym}{cur}{v / 1000:,.0f}k"
            return f"{sym}{cur}{v:,.0f}"

        if self.min is not None and self.max is not None and self.max != self.min:
            return f"{fmt(self.min)} - {fmt(self.max)}{per}"
        v = self.top
        if v is not None:
            return f"{fmt(v)}{per}"
        return self.raw or "salary stated"


# Query parameters that identify the visitor rather than the job.
#
# The whole query string used to be thrown away before hashing, which is
# right for these and catastrophic for everything else: an employer running
# Greenhouse behind their own careers page puts the posting id in the query,
# so `stripe.com/jobs/search?gh_jid=111` and `?gh_jid=999` hashed to the same
# id and the second one was never stored. Measured on the published UK shard:
# 2,383 of 41,038 rows disappeared into another role. Stripe published 89 and
# one survived; Bayada published 165 and one survived.
#
# It is invisible from the outside, which is what makes it the worst kind.
# There is no duplicate row to notice and no error: there is one Stripe job,
# and one Stripe job is exactly what a company with one vacancy looks like.
# `uid` is also the seen-set key, so "what is new" was broken for those
# employers on every ordinary scan too.
#
# A deny-list rather than an allow-list, deliberately. An unknown parameter is
# kept, so the worst an unrecognised tracking token can do is re-alert a role
# once, which is noisy and visible. An allow-list would drop an unrecognised
# IDENTIFYING parameter and merge two jobs into one, which is silent and is
# the bug being fixed. Noisy beats invisible.
#
# What survives is sorted, so a board that reorders its parameters does not
# re-alert everything it publishes.
_TRACKING = re.compile(
    r"^(?:utm_[a-z_]*|gh_src|ref|referer|referrer|source|src|fbclid|gclid"
    r"|msclkid|mc_cid|mc_eid|trk|trackingid|_ga|campaign|medium|lang|locale)$",
    re.I)


@dataclass
class Job:
    company: str
    title: str
    url: str
    platform: str
    location: str = ""
    remote: bool | None = None
    department: str | None = None
    posted_at: str | None = None
    description: str = ""
    salary: Salary = field(default_factory=Salary)
    sector: str | None = None
    country: str | None = None
    city: str = ""
    work_mode: str = "unstated"   # remote | hybrid | office | unstated
    # permanent | contract | unstated, from `employment.classify`. "unstated"
    # is the honest majority answer and is NOT a synonym for permanent: see
    # the module docstring for why reading it as one hides the contract
    # market from the reader.
    employment: str = "unstated"
    app_status: str = ""          # set from applications.local.yaml, if tracked
    source_id: str = ""

    # The id this role is STORED under, when it came out of the database.
    #
    # `uid` is derived from the URL, and the rule that derives it changes: a
    # row written before a change keeps the id it was written with, and
    # `rekey_uids` deliberately leaves url-less legacy rows alone. So a `Job`
    # rebuilt from a row and asked for its uid answered with a value matching
    # no row in the table, and that id then shipped in `out/roles.json` and
    # was matched against stored ids to decide what was new.
    stored_uid: str | None = None

    # populated downstream
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    @property
    def uid(self) -> str:
        """Stable id for the seen-set.

        Keyed on the apply URL where possible; ATS URLs carry a stable posting
        id. Falls back to company+title+location so a board that rewrites its
        URLs does not re-alert everything.
        """
        if self.stored_uid:
            return self.stored_uid
        basis = self.url or f"{self.company}|{self.title}|{self.location}"
        basis = re.sub(r"#.*$", "", basis.strip().lower())
        head, sep, query = basis.partition("?")
        if sep:
            keep = sorted(
                p for p in re.split(r"[&;]", query)
                if p and not _TRACKING.match(p.split("=", 1)[0]))
            basis = head + ("?" + "&".join(keep) if keep else "")
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["uid"] = self.uid
        d["salary_label"] = self.salary.label()
        return d


@dataclass
class Source:
    """One employer job board."""

    company: str
    url: str
    platform: str
    sector: str | None = None
    country: str | None = None
    domain: str | None = None  # used to verify the board is really this company
    method: str = "GET"
    body: dict[str, Any] | None = None
    keyword_template: bool = False   # url contains {keyword}, expanded per title
    # What this source's QUERY already asserts about employment type, for the
    # platforms that filter at the request and carry no per-result flag.
    #
    # Reed is the case this exists for. Its search API has a `contract=true`
    # parameter and no field on the row saying which kind of listing you are
    # looking at, so the only place the answer exists is the URL. Measured on
    # 6 September 2026 for "engineering manager": 418 results with
    # `contract=true`, 3,584 with `permanent=true`, 4,112 unfiltered, and ZERO
    # overlap between the first two pages. The text classifier reading the
    # same 100 known-contract rows labelled 57 of them and called one
    # permanent, which is a flatly wrong answer the query could have
    # prevented.
    #
    # Only ever set this from a filter somebody has VERIFIED filters. Two
    # LinkedIn sources were added and removed the same day for asserting a
    # job-type scope the endpoint ignored: `f_JT=T` returned the unfiltered
    # set, and `f_JT=C` returned a different set of FULL_TIME roles. A source
    # that lies here mislabels every row it returns, confidently.
    employment: str = ""
    # True when `adapters.prepare()` synthesised `method`/`body` from the URL
    # shape rather than reading them from the file. Writing derived values back
    # out made `save(load_file(x), x)` non-idempotent: the weekly prune of one
    # dead source produced a 529-line diff of Workday POST bodies, which is not
    # a pull request anybody can review.
    derived_request: bool = False

    @property
    def key(self) -> str:
        # Only the fragment is dropped. The query string is load-bearing on
        # some platforms: the LinkedIn guest endpoint distinguishes searches
        # purely by its parameters, so stripping them collapses six distinct
        # sources into one.
        return re.sub(r"#.*$", "", self.url)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "company": self.company,
            "url": self.url,
            "platform": self.platform,
        }
        for k in ("sector", "country", "domain"):
            if getattr(self, k):
                d[k] = getattr(self, k)
        if not self.derived_request:
            if self.method != "GET":
                d["method"] = self.method
            if self.body:
                d["body"] = self.body
        if self.keyword_template:
            d["keyword_template"] = True
        if self.employment:
            d["employment"] = self.employment
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Source":
        return cls(
            company=d["company"],
            url=d["url"],
            platform=d.get("platform") or "",
            sector=d.get("sector"),
            country=d.get("country"),
            domain=d.get("domain"),
            method=d.get("method", "GET"),
            body=d.get("body"),
            keyword_template=bool(d.get("keyword_template")),
            employment=(d.get("employment") or ""),
        )
