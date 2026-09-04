"""One parser per ATS. Each takes a raw payload and yields normalised `Job`s.

Adding a platform: write a `parse_<name>(payload, src)` generator, add it to
REGISTRY in __init__.py with a URL pattern, and add a builder in
`jobradar.discover` if the token can be found from a careers page.

Notes on the awkward ones are inline. They are all things that cost a
debugging session to find out.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from urllib.parse import unquote, urljoin, urlparse

from ..models import Job, Salary, Source
from ..employment import from_platform
from ..salary import (from_adzuna, from_ashby, from_greenhouse, from_pinpoint,
                       from_reed, parse_text)

_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


class BoardUnreadable(Exception):
    """The board answered, and what it answered is not a job listing.

    Raised by a parser that can see the difference between "this employer has
    no vacancies today" and "this page is not the listing at all". Those two
    reach the rest of the tool as the same number -- zero postings -- and only
    one of them is a fact about the employer. `validate --prune` deletes on
    the first reading, so a board that answers HTTP 200 with a login wall gets
    removed from the shipped source list for having said nothing.

    `discover._parse_or_why` already turns any exception out of a parser into
    "could not be read", which is the verdict that is never pruned, and
    `adapters.parse` now says so out loud instead of silently returning an
    empty list. So a parser raising this is the way to be heard.
    """


def _text(v: Any) -> str:
    """Flatten whatever an API returned into a readable string.

    Some platforms wrap a field as {"rendered": "Data Engineer"}. Passing that
    to str() put a Python dict repr on screen as a job title.
    """
    if not v:
        return ""
    if isinstance(v, dict):
        for k in ("rendered", "name", "label", "text", "value"):
            if isinstance(v.get(k), str):
                v = v[k]
                break
        else:
            v = " ".join(str(x) for x in v.values() if isinstance(x, str))
    elif isinstance(v, (list, tuple)):
        v = ", ".join(str(x) for x in v if isinstance(x, str))
    s = html.unescape(str(v))
    s = _TAGS.sub(" ", s)
    return _WS.sub(" ", s).strip()


def _iso(v: Any) -> str | None:
    """Normalise the six date formats these APIs between them use."""
    if not v:
        return None
    if isinstance(v, (int, float)):
        # Milliseconds vs seconds: anything past ~2001 in ms is > 1e12.
        ts = float(v) / 1000.0 if float(v) > 1e11 else float(v)
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    s = str(v).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d/%m/%Y",
                # NHS Jobs writes the month in full ("18 August 2026"). Without
                # %B every NHS role had no date, so the recency points never
                # fired and 28 roles clumped onto three scores.
                "%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y",
                # Taleo lets each career section pick its own date format, and
                # they really do differ: TTEC and D.R. Horton send
                # "Aug 24, 2026", Transport for London sends "13-Aug-26". A
                # Taleo posting is only ever found by shape, so a format this
                # cannot read is a posting with no date and no recency points.
                "%d-%b-%y", "%d-%b-%Y",
                # RFC 822, which is what every RSS <pubDate> is:
                # "Wed, 19 Aug 2026 16:47:00 +0100". Without it no feed-shaped
                # source had a date at all, so the recency points never fired
                # for any of them and every Teamtailor role scored as undated.
                "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S"):
        try:
            return datetime.strptime(s.replace("Z", "+0000") if fmt.endswith("%z") else s,
                                     fmt).date().isoformat()
        except ValueError:
            continue
    m = re.search(r"\d{4}-\d{2}-\d{2}", s)
    return m.group(0) if m else None


def _remote(*bits: Any) -> bool | None:
    blob = " ".join(str(b) for b in bits if b).lower()
    if not blob:
        return None
    if re.search(r"\bremote\b|\bwork from home\b|\bwfh\b|\bdistributed\b", blob):
        return not re.search(r"\bnon.?remote\b|\bno remote\b|\bhybrid only\b", blob)
    return None


# --------------------------------------------------------------------------
# Greenhouse
# --------------------------------------------------------------------------
# Greenhouse's `location.name` is a free-text box the employer fills in, and
# some of them fill it in with the working arrangement instead of a place.
# Cloudflare are the clearest case: 247 of their 306 open roles state
# "Hybrid" or "In-Office" there and nothing else, and Stripe put "N/A" on 21.
# `offices` on those same rows names the actual city -- Washington DC, Austin
# TX, "Canada Locations" -- and it was only consulted when `location` was
# empty, which these are not. So four in five Cloudflare roles reached the
# country filter with a work pattern where their location should be, and a
# search for UK roles could neither keep them nor rule them out.
#
# Two sets, because the right repair differs. A work pattern is worth keeping
# and worth siting, so the office is added to it. A placeholder says nothing
# at all and is replaced outright.
_GH_WORK_PATTERN = {"hybrid", "in-office", "in office", "onsite", "on-site",
                    "in-person", "in person", "office", "office-based"}
_GH_NO_ANSWER = {"n/a", "na", "none", "tbd", "tbc", "-", "various", "multiple"}


def parse_greenhouse(payload: Any, src: Source) -> Iterator[Job]:
    """`pay_input_ranges` appears ONLY with `?pay_transparency=true`.
    `content=true` is a separate parameter and does not trigger it.
    Also: never send a body with the GET, Greenhouse answers 403 if you do.
    """
    for j in (payload or {}).get("jobs", []) or []:
        loc = j.get("location") or {}
        stated = loc.get("name") if isinstance(loc, dict) else _text(loc)
        location = stated
        offices = ", ".join(
            o.get("name", "") for o in (j.get("offices") or []) if isinstance(o, dict)
        )
        flat = (stated or "").strip().lower()
        if offices and flat in _GH_WORK_PATTERN:
            location = f"{stated.strip()}, {offices}"
        elif offices and flat in _GH_NO_ANSWER:
            location = offices
        desc = _text(j.get("content"))
        sal = from_greenhouse(j.get("pay_input_ranges"))
        if not sal.confirmed:
            sal = parse_text(desc[:1500])
        yield Job(
            company=j.get("company_name") or src.company,
            title=_text(j.get("title")),
            url=j.get("absolute_url") or "",
            platform="greenhouse",
            location=_text(location or offices),
            remote=_remote(location, offices, j.get("title")),
            department=", ".join(
                d.get("name", "") for d in (j.get("departments") or []) if isinstance(d, dict)
            ) or None,
            posted_at=_iso(j.get("first_published") or j.get("updated_at")),
            description=desc,
            salary=sal,
            source_id=src.key,
        )


# --------------------------------------------------------------------------
# Ashby
# --------------------------------------------------------------------------
def parse_ashby(payload: Any, src: Source) -> Iterator[Job]:
    """Returns HTTP 200 with an empty `jobs` array for a token that does not
    exist AND for one being rate-limited. Validate on job count, never status.
    Compensation needs `?includeCompensation=true`.
    """
    for j in (payload or {}).get("jobs", []) or []:
        if j.get("isListed") is False:
            continue
        secondary = ", ".join(
            s.get("location", "") if isinstance(s, dict) else str(s)
            for s in (j.get("secondaryLocations") or [])
        )
        loc = _text(j.get("location"))
        yield Job(
            company=src.company,
            title=_text(j.get("title")),
            url=j.get("jobUrl") or j.get("applyUrl") or "",
            platform="ashby",
            location=", ".join(x for x in (loc, secondary) if x),
            remote=j.get("isRemote") if isinstance(j.get("isRemote"), bool)
            else _remote(loc, secondary),
            department=_text(j.get("department") or j.get("team")) or None,
            posted_at=_iso(j.get("publishedAt")),
            description=_text(j.get("descriptionPlain") or j.get("descriptionHtml")),
            salary=from_ashby(j.get("compensation")),
            # The employer's own answer, not a guess at their prose. See
            # `employment.from_platform` for what the values mean and why
            # "FullTime" is not read as permanent.
            employment=from_platform(j.get("employmentType")),
            source_id=src.key,
        )


# --------------------------------------------------------------------------
# Lever
# --------------------------------------------------------------------------
def parse_lever(payload: Any, src: Source) -> Iterator[Job]:
    """Lever returns a bare top-level list, not an object with a `jobs` key."""
    items = payload if isinstance(payload, list) else (payload or {}).get("data", [])
    for j in items or []:
        cats = j.get("categories") or {}
        loc = _text(cats.get("location"))
        desc = _text(j.get("descriptionPlain") or j.get("description"))
        extra = " ".join(
            _text(l.get("content")) for l in (j.get("lists") or []) if isinstance(l, dict)
        )
        yield Job(
            company=src.company,
            title=_text(j.get("text")),
            url=j.get("hostedUrl") or j.get("applyUrl") or "",
            platform="lever",
            location=loc,
            remote=_remote(loc, cats.get("commitment"), j.get("workplaceType")),
            department=_text(cats.get("team") or cats.get("department")) or None,
            posted_at=_iso(j.get("createdAt")),
            description=(desc + " " + extra).strip(),
            salary=parse_text(f"{desc[:1500]} {extra[:500]}"),
            # Free text an employer types, so "Full time days" and "Full time,
            # Part time, and Weekend shifts available." both turn up here.
            employment=from_platform(cats.get("commitment")),
            source_id=src.key,
        )


# --------------------------------------------------------------------------
# Workable
# --------------------------------------------------------------------------
def _workable_where(j: dict) -> tuple[str, bool | None]:
    """Where a Workable posting is, from the fields the widget actually sends.

    This read `j["location"]` for a dict of city/region/country. The widget
    has never had a `location` key: it sends `city`, `state` and `country` at
    the top level, and a `locations` array carrying `countryCode` as well. So
    the dict was always empty and every posting from these 2,094 boards was
    stored with no location at all -- 2,509 of a 5,479-posting sample, which
    was 93% of everything the tool could not place in a country. A role with
    no location cannot be filtered by country, which is the first thing
    anybody asks of a job search.

    `locations` first, because it is structured and carries the country code
    rather than a country name to be matched by spelling. Hidden entries are
    Workable's way of marking a location the employer does not advertise, so
    they are skipped. Several are joined with " / ", which is the separator
    the country logic already reads as a role open in more than one place.
    """
    out = []
    for loc in (j.get("locations") or []):
        if not isinstance(loc, dict) or loc.get("hidden"):
            continue
        part = ", ".join(str(loc[k]) for k in ("city", "region", "country")
                         if loc.get(k))
        if part:
            out.append(part)
    if not out:
        part = ", ".join(str(j[k]) for k in ("city", "state", "country")
                         if j.get(k))
        if part:
            out.append(part)
    remote = bool(j.get("telecommuting")) or any(
        (loc.get("workplace") == "remote")
        for loc in (j.get("locations") or []) if isinstance(loc, dict))
    return " / ".join(dict.fromkeys(out)), (True if remote else None)


def parse_workable(payload: Any, src: Source) -> Iterator[Job]:
    for j in (payload or {}).get("jobs", []) or []:
        location, remote = _workable_where(j)
        desc = _text(j.get("description"))
        yield Job(
            company=src.company,
            title=_text(j.get("title")),
            url=j.get("url") or j.get("application_url") or j.get("shortlink") or "",
            platform="workable",
            location=location,
            remote=remote if isinstance(remote, bool) else _remote(location),
            department=_text(j.get("department")) or None,
            posted_at=_iso(j.get("published_on") or j.get("created_at")),
            description=desc,
            salary=parse_text(desc[:1500]),
            # Two spellings, because the widget endpoint and the account
            # endpoint disagree. The first live board checked had 22 postings
            # and 10 of them typed "Contract", every one of which this tool
            # had been storing as "unstated".
            employment=from_platform(j.get("employment_type")
                                     or j.get("employmentType")),
            source_id=src.key,
        )


# --------------------------------------------------------------------------
# Workable, the other way round
#
# The 2,094 Workable boards on the bundled list are read one employer at a
# time, which is 2,094 requests to one host every scan. Workable's own answer
# to that was a 429 with `Retry-After: 57841`, a sixteen hour refusal, and the
# per-host pacing that stopped it costing 8.7 hours now costs fifty minutes
# instead: 2,094 requests at 0.7 a second is the floor of the whole scan, and
# nothing about a wider worker pool changes it.
#
# jobs.workable.com is Workable's own aggregator over every board it hosts,
# and it has a search API. One query for "engineering manager" in the United
# Kingdom returns 110 postings in six requests, each carrying the company, a
# structured location with a country name, the workplace mode and the full
# description HTML. Six requests against 2,094.
#
# It is also strictly more than the boards give. The bundled 2,094 are the
# Workable employers a Common Crawl harvest happened to find; the search
# reaches every Workable employer, including the ones nobody has crawled.
#
# What it does not give is salary, which no Workable board gives either, so
# `parse_text` reads the description exactly as the board parser does.
# --------------------------------------------------------------------------
def parse_workable_search(payload: Any, src: Source) -> Iterator[Job]:
    """jobs.workable.com/api/v1/jobs, the aggregator rather than one board.

    The company is the payload's own, not the source's. Every other parser
    here takes `src.company`, because for a board the source IS the employer.
    Here the source is a search, so taking `src.company` would file all 110
    results under the literal string "Workable search: engineering manager"
    and dedupe would then treat unrelated employers as one.
    """
    for j in (payload or {}).get("jobs", []) or []:
        co = j.get("company") or {}
        company = _text(co.get("title")) if isinstance(co, dict) else _text(co)
        if not company:
            # No employer name is not a role we can show: the dashboard groups
            # by employer and the dedupe rule that prefers a direct board over
            # an aggregator cannot run without one.
            continue
        loc = j.get("location") or {}
        if isinstance(loc, dict):
            location = ", ".join(
                str(loc.get(k)) for k in ("city", "subregion", "countryName")
                if loc.get(k))
        else:
            location = _text(loc)
        # `workplace` is Workable's own flag and is one of remote, hybrid or
        # on-site. Trusted over reading the location text, which is what
        # `_remote` falls back to, because a hybrid role in London reads as
        # neither and guessing it wrong either hides a role or offers one that
        # cannot be done from where the user lives.
        workplace = (_text(j.get("workplace")) or "").lower()
        desc = _text(j.get("description"))
        yield Job(
            company=company,
            title=_text(j.get("title")),
            url=j.get("url") or "",
            # "workable_search", not "workable". They are the same company's
            # data, but the URL here is a jobs.workable.com view page, not the
            # employer's own board, and `directness` has to be able to tell
            # them apart so a role found both ways keeps the employer's link
            # and not the aggregator's. dedupe groups on employer and title,
            # so they still meet; this only decides which one wins.
            platform="workable_search",
            location=location,
            remote=True if workplace == "remote" else (
                # "on_site" with an underscore is what Workable actually
                # sends. Checked 2026-08-27 over 80 postings from the search,
                # the day feed and a company board: every one was `remote`,
                # `hybrid` or `on_site`, and nothing was ever "on-site" or
                # "onsite". Without the underscore spelling an on-site role
                # fell through to reading its location text, which is the
                # guess this flag exists to avoid, and "Remote, Oregon" or a
                # title mentioning remote work then marked it remote.
                False if workplace in ("hybrid", "on_site", "on-site", "onsite")
                else _remote(location)),
            department=_text(j.get("department")) or None,
            posted_at=_iso(j.get("created") or j.get("updated")),
            description=desc,
            salary=parse_text(desc[:1500]),
            source_id=src.key,
        )


# --------------------------------------------------------------------------
# Workable, one employer at a time, on the other host
#
# `jobs.workable.com/api/v1/companies/<uuid>` is the same board the widget
# serves, read from the aggregator's host instead of the boards' host. It is
# the only alternative path to a Workable board that was found to exist, and
# it is worth having because apply.workable.com is not merely slow, it is
# unreliable: `fetch.PER_HOST_RPS` records 41 of 419 boards (9.8%) coming back
# 429 in a run paced at the 0.7/s that host is supposed to tolerate.
#
# Measured 2026-08-27, while a full scan was saturating apply.workable.com at
# that same moment: forty of these back to back at 2.83 requests a second, no
# refusal, 0.3s a response. So the two hosts do not share a budget, which is
# what `fetch_workable_search` already assumed and nobody had shown.
#
# It also arrives with the advert on it. The widget URL the bundled list uses
# has no `details=true`, and without that the response carries no description
# at all: 3,035 bytes for a three-role board against 28,751 with it. So a
# board read on apply costs a request now and an enrichment fetch later.
#
# Three things it is not.
#
# It is not complete. 25 employers were read both ways on 2026-08-27, 1,058
# postings through the widget and 1,025 through here. 21 matched exactly and
# 23 lost nothing, but the two that lost postings lost real ones: SPD
# Technology's board has 61 roles and this returns 25, and among the 36 it
# drops are "Senior Engineering Manager" and "Senior Data Engineer". Workable
# lets an employer publish a role to their own careers page without publishing
# it to jobs.workable.com, and `isHidden` on the company says a whole employer
# can sit out too. 4% of postings went missing over the sample. That is the
# reason this is an addition and not a replacement.
#
# It pages twenty at a time behind `nextPageToken` where the widget hands back
# the whole board in one response, so an employer with more than twenty open
# roles costs more requests here than there. Measured on 40 bundled boards
# rather than on a sample drawn from postings, which oversamples large
# employers: median 6.5 roles, 9 of 40 over twenty, 53 pages against 40 widget
# reads. So the honest saving is 2,764 requests at 3/s against 2,094 at 0.7/s,
# fifteen minutes against fifty, not the fourfold the host rates suggest.
#
# And it cannot find itself. The address needs Workable's own account UUID,
# and no route was found from the `apply.workable.com/<slug>` the bundled list
# holds to that UUID. `/api/v1/companies/<slug>` answers "Company not found",
# so does the numeric account id the embed widget uses, and the UUID is absent
# from the job payload, the single-job payload and the application-form
# payload. Searching on the employer's name found 17 of a sample of 40 bundled
# employers, and one of those 17 was a different company with a similar name.
# So this parses a board whose UUID is already known. It is not, on its own, a
# way to stop reading the 2,094.
# --------------------------------------------------------------------------
def parse_workable_company(payload: Any, src: Source) -> Iterator[Job]:
    """jobs.workable.com/api/v1/companies/<uuid>: one employer's whole board.

    The payload is the employer wrapped around a `jobs` list, where the search
    is a `jobs` list with the employer repeated inside every item. Each item
    here carries its own `company` too, so the top-level one is only a
    fallback, and `src.company` is the last resort: the bundled list spells
    employers from their board slug ("Cqs", "Instanda") and Workable spells
    them as the employer registered them ("CQS SA", "INSTANDA"). Preferring
    Workable's own spelling is what lets a role found this way and the same
    role found through the search meet each other in `dedupe`.

    Deliberately emits `platform="workable_search"` rather than a name of its
    own. `screen.directness` has to score this below an employer's own board,
    for the reason it already scores the search there -- the link handed to
    the reader is a jobs.workable.com view page, not the employer's apply page
    -- and an unlisted platform name defaults to 2, which would let this beat
    the real board. Same host, same view URLs, same standing: same name.
    """
    if not isinstance(payload, dict):
        return
    raw_top = payload.get("company")
    top = raw_top if isinstance(raw_top, dict) else payload
    top_name = _text(top.get("title")) if isinstance(top, dict) else ""
    for j in payload.get("jobs", []) or []:
        if not isinstance(j, dict):
            continue
        # `state` is "published" on everything a board serves. A payload that
        # ever carries a draft must not put it in front of a reader, and an
        # absent field is not evidence of one.
        if j.get("state") and j["state"] != "published":
            continue
        co = j.get("company") or {}
        name = _text(co.get("title")) if isinstance(co, dict) else _text(co)
        company = name or top_name or src.company
        if not company:
            continue
        loc = j.get("location") or {}
        if isinstance(loc, dict):
            location = ", ".join(
                str(loc.get(k)) for k in ("city", "subregion", "countryName")
                if loc.get(k))
        else:
            location = _text(loc)
        workplace = (_text(j.get("workplace")) or "").lower()
        desc = _text(j.get("description"))
        yield Job(
            company=company,
            title=_text(j.get("title")),
            url=j.get("url") or "",
            platform="workable_search",
            location=location,
            remote=True if workplace == "remote" else (
                # `on_site` with the underscore: see parse_workable_search.
                False if workplace in ("hybrid", "on_site", "on-site", "onsite")
                else _remote(location)),
            department=_text(j.get("department")) or None,
            posted_at=_iso(j.get("created") or j.get("updated")),
            description=desc,
            salary=parse_text(desc[:1500]),
            source_id=src.key,
        )


# --------------------------------------------------------------------------
# SmartRecruiters
# --------------------------------------------------------------------------
def parse_smartrecruiters(payload: Any, src: Source) -> Iterator[Job]:
    """Like Ashby: 200 + empty `content` for a company id that does not exist."""
    for j in (payload or {}).get("content", []) or []:
        loc = j.get("location") or {}
        location = ", ".join(
            str(loc.get(k)) for k in ("city", "region", "country") if loc.get(k)
        ) if isinstance(loc, dict) else _text(loc)
        cid = j.get("id") or ""
        # `ref` is the API URL. Swapping the host in it produced
        # jobs.smartrecruiters.com/<co>/postings/<id>, which 404s: the public
        # path has no /postings/ segment. Every link the tool offered for this
        # platform was dead, which is worse than not listing the role, because
        # a dead link is only discovered after someone decides to apply.
        ident = _text((j.get("company") or {}).get("identifier")) or src.company
        url = (f"https://jobs.smartrecruiters.com/{ident}/{cid}" if cid
               else (j.get("ref") or ""))
        yield Job(
            company=_text((j.get("company") or {}).get("name")) or src.company,
            title=_text(j.get("name")),
            url=url,
            platform="smartrecruiters",
            location=location,
            remote=bool(loc.get("remote")) if isinstance(loc, dict) else _remote(location),
            department=_text((j.get("department") or {}).get("label")) or None,
            posted_at=_iso(j.get("releasedDate") or j.get("createdOn")),
            description=_text(j.get("jobAd")),
            salary=Salary(),
            # `{"id": "permanent", "label": "Full-time"}`. The id is the
            # machine value; the label is display text that has already been
            # through a translation table, so the id is what to read.
            employment=from_platform(j.get("typeOfEmployment")),
            source_id=src.key,
        )


# --------------------------------------------------------------------------
# Workday (CXS)
# --------------------------------------------------------------------------
# Workday states a posting's AGE, never its date: `postedOn` is
# "Posted 19 Days Ago", and across 669 postings from five tenants it took
# exactly four shapes -- "Posted Today" (76), "Posted Yesterday" (3),
# "Posted N Days Ago" (362) and "Posted N+ Days Ago" (228). None of them is a
# date, so `_iso` returned None for all of them and every posting from all
# 1,489 Workday boards arrived undated and scored as though it had no
# recency. Barclays, HSBC, Nvidia and Adobe are all on this platform.
_WD_AGE = re.compile(r"(\d+)\s*\+?\s*days?\s+ago", re.I)


def _workday_posted(value: Any, today=None) -> str | None:
    """A date from Workday's relative phrasing.

    "30+ Days Ago" is read as exactly thirty days, which is the oldest date
    the phrase permits and therefore the least flattering reading of it: the
    posting is at LEAST that old. Rounding the other way would let a role that
    has been open for a year collect recency points.
    """
    text = str(value or "").strip()
    if not text:
        return None
    exact = _iso(text)
    if exact:
        return exact
    low = text.lower()
    if "today" in low:
        days = 0
    elif "yesterday" in low:
        days = 1
    else:
        m = _WD_AGE.search(low)
        if not m:
            return None
        days = int(m.group(1))
    base = today or datetime.now(timezone.utc).date()
    return (base - timedelta(days=days)).isoformat()


# Workday collapses a multi-location posting to a count: `locationsText` is
# the literal string "2 Locations". Stored as-is it becomes a place name that
# no country logic can read and that no reader can tell from a real one.
_WD_COUNT = re.compile(r"^\s*(\d+)\s+Locations?\s*$", re.I)


def parse_workday(payload: Any, src: Source) -> Iterator[Job]:
    """POST, not GET. Body: {"appliedFacets":{},"limit":20,"offset":0,"searchText":""}

    A tenant that does not exist answers 406, not 404, because of wildcard DNS
    on *.myworkdayjobs.com. So a non-404 response proves nothing about whether
    the tenant is real; only `jobPostings` having entries does.
    """
    base = re.sub(r"/wday/cxs/.*$", "", src.url)
    m = re.search(r"/wday/cxs/([^/]+)/([^/]+)/jobs", src.url)
    site = m.group(2) if m else ""
    for j in (payload or {}).get("jobPostings", []) or []:
        path = j.get("externalPath") or ""
        url = urljoin(f"{base}/en-US/{site}/", path.lstrip("/")) if path else base
        bullets = " ".join(str(b) for b in (j.get("bulletFields") or []))
        loc = _text(j.get("locationsText"))
        # "2 Locations" is a count, not a place, and it was being stored as
        # one. Across 12 real tenants, 198 postings said this and every one of
        # them rendered on the dashboard with "2 Locations" where a city
        # should be, which reads as a location rather than as the absence of
        # one. Workday's own `externalPath` names the primary location, and it
        # resolved to a country for 192 of the 198.
        more = _WD_COUNT.match(loc)
        if more:
            loc = ""
        if not loc:
            # Some tenants leave locationsText empty and put the city in the
            # path instead. Without this the role has no location at all, and
            # an unknown country passes a country filter it should fail.
            m2 = re.search(r"/job/([^/]+)/", path or "")
            if m2:
                loc = _text(m2.group(1).replace("-", " "))
            if not loc:
                loc = " ".join(str(b) for b in (j.get("bulletFields") or [])[:2])
        yield Job(
            company=src.company,
            title=_text(j.get("title")),
            url=url,
            platform="workday",
            location=loc,
            # Said out loud, because the location shown is the primary one and
            # a role open in London and New York must not look like a role
            # open only in whichever of them Workday put in the path.
            flags=([f"listed in {more.group(1)} locations; "
                    f"the one shown is Workday's primary"] if more else []),
            remote=_remote(loc, j.get("title")),
            department=None,
            posted_at=_workday_posted(j.get("startDate") or j.get("postedOn")),
            description=bullets,
            salary=parse_text(bullets),
            source_id=src.key,
        )


# --------------------------------------------------------------------------
# LinkedIn (public guest endpoint)
# --------------------------------------------------------------------------
_LI_CARD = re.compile(r"<li>(.*?)</li>", re.S)
_LI_TITLE = re.compile(r'base-search-card__title"[^>]*>(.*?)<', re.S)
_LI_CO = re.compile(r'base-search-card__subtitle"[^>]*>\s*(?:<a[^>]*>)?(.*?)<', re.S)
_LI_LOC = re.compile(r'job-search-card__location"[^>]*>(.*?)<', re.S)
_LI_URL = re.compile(r'href="(https://[^"]*?/jobs/view/[^"?]+)')
_LI_DATE = re.compile(r'datetime="([\d-]+)"')


def parse_linkedin(payload: Any, src: Source) -> Iterator[Job]:
    """The guest `seeMoreJobPostings/search` endpoint returns server-rendered
    HTML cards to a plain GET, no login and no JS. It gives title, company,
    location and a canonical /jobs/view/ URL, but no description or salary,
    so these are lead-generation rather than screenable postings.
    """
    text = payload if isinstance(payload, str) else ""
    for card in _LI_CARD.findall(text):
        t = _LI_TITLE.search(card)
        c = _LI_CO.search(card)
        u = _LI_URL.search(card)
        if not (t and u):
            continue
        loc = _LI_LOC.search(card)
        d = _LI_DATE.search(card)
        yield Job(
            company=_text(c.group(1)) if c else src.company,
            title=_text(t.group(1)),
            url=u.group(1),
            platform="linkedin",
            location=_text(loc.group(1)) if loc else "",
            remote=_remote(loc.group(1) if loc else "", t.group(1)),
            posted_at=_iso(d.group(1)) if d else None,
            description="",
            salary=Salary(),
            source_id=src.key,
            flags=["listing-only: no description available from this source"],
        )


# --------------------------------------------------------------------------
# Recruitee
# --------------------------------------------------------------------------
def parse_recruitee(payload: Any, src: Source) -> Iterator[Job]:
    """Recruitee's `/api/offers/`.

    `careers_url` is not the board's own address: it is whatever vanity domain
    the employer pointed at Recruitee, and that domain outlives the board it
    used to serve. Makersite's offers all state
    `https://makersite.io/o/<slug>`, which is HTTP 404 today, while
    `https://makersitegmbh.recruitee.com/o/<slug>` is 200 with the advert on it
    (both checked live, 2026-08-25). The published field is the broken one.

    So the link is rebuilt on the host we have just fetched successfully,
    which is the only host in the exchange with evidence behind it. Falls back
    to `careers_url` when there is no slug to build from.
    """
    host = urlparse(src.url).netloc
    for j in (payload or {}).get("offers", []) or []:
        loc = ", ".join(
            str(j.get(k)) for k in ("city", "country") if j.get(k)
        ) or _text(j.get("location"))
        desc = _text(j.get("description")) + " " + _text(j.get("requirements"))
        slug = _text(j.get("slug"))
        yield Job(
            company=src.company,
            title=_text(j.get("title")),
            url=(f"https://{host}/o/{slug}" if host and slug
                 else (j.get("careers_url") or j.get("careers_apply_url") or "")),
            platform="recruitee",
            location=loc,
            remote=_remote(loc, j.get("remote")),
            department=_text(j.get("department")) or None,
            posted_at=_iso(j.get("published_at") or j.get("created_at")),
            description=desc.strip(),
            salary=parse_text(desc[:1500]),
            # "fulltime_permanent", "contract", "freelance", "internship".
            employment=from_platform(j.get("employment_type_code")
                                     or j.get("employment_type")),
            source_id=src.key,
        )


# --------------------------------------------------------------------------
# Breezy HR
# --------------------------------------------------------------------------
# Breezy writes countries as ISO 3166 alpha-2, so the United Kingdom arrives
# as "GB". Everything downstream of the adapters speaks screen.py's
# vocabulary, in which that country is "UK". Handing "GB" straight through
# filed every British posting under a code no country filter, dashboard facet
# or `--country` flag ever asks for, which loses a whole board from a UK-only
# search without reporting anything.
_BZ_COUNTRY = {"GB": "UK"}


def _breezy_place(loc: Any) -> str:
    """One Breezy location, written the way the rest of the tool reads them.

    Deliberately rebuilt from the parts rather than taken from Breezy's own
    `location.name`, which renders as "Lambeth, GB". The bare alpha-2 code
    reads badly on screen, and the country's full name is the stronger signal
    for the location filter, which looks for "United Kingdom" before it falls
    back to two-letter forms.
    """
    if not isinstance(loc, dict):
        return _text(loc)
    state = loc.get("state") or {}
    country = loc.get("country") or {}
    parts: list[str] = []
    seen: set[str] = set()
    for p in (_text(loc.get("city")),
              _text(state.get("id") or state.get("name")),
              _text(country.get("name"))):
        if p and p.lower() not in seen:
            seen.add(p.lower())
            parts.append(p)
    return ", ".join(parts) or _text(loc.get("name"))


def parse_breezy(payload: Any, src: Source) -> Iterator[Job]:
    """Breezy HR. The board is `https://<company>.breezy.hr/json`.

    Like Lever it answers with a bare top-level list, not an object with a
    `jobs` key. Like Ashby it answers 200 with an empty list for a token that
    does not exist, so liveness is a job count and never a status code.

    The list carries no description whatsoever, only metadata, which is why
    `enrich` grew a Breezy fetcher: the posting page embeds the full advert as
    schema.org JSON-LD. What the list does carry is a ready-formatted salary
    string ("£35,000 – £40,000 / year"), so a fair share of these state pay.
    """
    items = payload if isinstance(payload, list) else (payload or {}).get("positions") or []
    for j in items or []:
        if not isinstance(j, dict):
            continue
        primary = j.get("location") if isinstance(j.get("location"), dict) else {}
        places = [p for p in (j.get("locations") or []) if isinstance(p, dict)] \
            or ([primary] if primary else [])

        names: list[str] = []
        seen: set[str] = set()
        for p in places:
            txt = _breezy_place(p)
            # Breezy repeats the same place in `locations` when an employer
            # ticks two identical remote entries, which produced
            # "Remote / Remote" on a real Dozuki posting.
            if txt and txt.lower() not in seen:
                seen.add(txt.lower())
                names.append(txt)
        # Joined with " / " and not ", ": screen.py splits a multi-location
        # string on the slash but treats a comma as binding a place to its
        # qualifier, so a comma here fuses "Philadelphia, PA" and "Salt Lake
        # City, UT" into one string that resolves to neither.
        location = " / ".join(names)

        # Only set the country when the posting names exactly one. Where it
        # names several, leaving it unset lets screen.py mark it "multiple"
        # from the location string rather than picking a winner here.
        codes = {str((p.get("country") or {}).get("id") or "").upper() for p in places}
        codes.discard("")
        country = None
        if len(codes) == 1:
            code = codes.pop()
            country = _BZ_COUNTRY.get(code, code)

        remote_details = primary.get("remote_details") or {}
        detail = _text(remote_details.get("value")).lower()
        label = _text(remote_details.get("label"))
        if detail == "hybrid":
            # `is_remote` is true for hybrid postings as well as remote ones.
            # Taking it at face value marked a Bournemouth role that wants you
            # in the office part of the week as remote, which is the single
            # thing a remote filter must never do.
            remote: bool | None = False
        elif detail == "remote" or primary.get("is_remote") is True:
            remote = True
        else:
            remote = _remote(location, j.get("name"))

        pay = _text(j.get("salary"))
        sal = parse_text(pay)
        if sal.confirmed:
            sal.raw = pay[:120]

        url = _text(j.get("url"))
        if not url and j.get("friendly_id"):
            host = urlparse(src.url).netloc or \
                f"{_text((j.get('company') or {}).get('friendly_id'))}.breezy.hr"
            url = f"https://{host}/p/{j['friendly_id']}"

        # There is no advert text here, so the only thing worth screening is
        # the metadata. The remote label earns its place: "Hybrid (Some
        # remote, some in person)" is what makes screen.py file the role as
        # hybrid rather than reading the word "remote" off the location.
        meta = [x for x in (_text((j.get("type") or {}).get("name")),
                            _text(j.get("department")), label, pay) if x]

        yield Job(
            # The board publishes the employer's own name, and `discover`
            # checks a board's identity against it. Falling back to src.company
            # would make every board agree with whatever we already believed.
            company=_text((j.get("company") or {}).get("name")) or src.company,
            title=_text(j.get("name")),
            url=url,
            platform="breezy",
            location=location,
            remote=remote,
            department=_text(j.get("department")) or None,
            posted_at=_iso(j.get("published_date")),
            description=". ".join(meta),
            salary=sal,
            country=country,
            source_id=src.key,
        )


# --------------------------------------------------------------------------
# Personio (XML)
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# Jobvite
# --------------------------------------------------------------------------
# There is no public JSON here. `/<company>/jobs.json`, `/search/jobs` and
# `/jobs.rss` all return the same career-site HTML, and `api/v1/jobs` redirects
# away. The board is server-rendered though, so no browser is needed: the list
# is a plain table of links.
#
# The markup is employer-customisable and really does differ. NinjaOne ship
# `<td class="jv-job-list-name">` and LHH ship `<div class="jv-job-list-name">`
# for the same thing, so the class names are the anchor and the element name
# is not. The location cell is closed on `</td>` or `</div>` specifically,
# because NinjaOne put a `<span>,</span>` inside it and a lazier close would
# cut the location off after the first word.
_JV_ROW = re.compile(
    r'class="[^"]*jv-job-list-name[^"]*"[^>]*>\s*<a\s+href="([^"]+)"[^>]*>(.*?)</a>'
    r'\s*</(?:td|div)>\s*'
    r'<(?:td|div)[^>]*class="[^"]*jv-job-list-location[^"]*"[^>]*>(.*?)</(?:td|div)>',
    re.S | re.I,
)
_JV_HEAD = re.compile(r"<h3[^>]*>(.*?)</h3>", re.S | re.I)

# The location cell carries the working arrangement in front of the place, and
# "Hybrid Remote" contains the word "remote". Reading it with the usual
# keyword check returns True, which would have marked all 31 hybrid roles on
# NinjaOne's board as remote. This is the same failure Breezy's `is_remote`
# caused on an office-based Bournemouth job, arriving by a different route.
_JV_HYBRID = re.compile(r"^\s*hybrid\s+remote\b\s*,?\s*", re.I)
_JV_REMOTE = re.compile(r"^\s*remote\b\s*,?\s*", re.I)


# Split at the meta div rather than matching a closed one. `_JV_ROW` captures
# the location cell non-greedily and stops at the first `</div>`, so the
# closing tag is not inside the captured text and a pattern requiring one
# matches nothing at all -- which looks exactly like a cell that had no meta
# div in it.
_JV_META = re.compile(r'<div[^>]*class="[^"]*jv-meta[^"]*"[^>]*>', re.I)
_JV_META_COUNT = re.compile(
    r'<div[^>]*class="[^"]*jv-meta[^"]*"[^>]*>\s*(\d+)\s+Locations?\b',
    re.S | re.I)


def parse_jobvite(payload: Any, src: Source) -> Iterator[Job]:
    """Jobvite. The board is `https://jobs.jobvite.com/<company>/jobs`.

    A company that does not exist answers 302 and lands somewhere with no job
    rows in it, so following redirects turns "no such board" into a perfectly
    ordinary 200. Liveness is the job count, as everywhere else here.

    The list carries no advert text, no date and no salary, so `enrich` reads
    the posting page's schema.org JSON-LD, which Jobvite publishes on every
    job for Google Jobs.
    """
    text = payload if isinstance(payload, str) else ""

    # Department comes from the nearest `<h3>` above the row, which is how
    # these boards group their tables. Checked against both live boards: it
    # yields real department names on each and never picks up the sidebar
    # headings, which sit above the first table rather than between tables.
    heads = [(m.start(), _text(m.group(1))) for m in _JV_HEAD.finditer(text)]

    for m in _JV_ROW.finditer(text):
        title = _text(m.group(2))
        if not title:
            continue

        # Jobvite collapses a multi-location role to a count in its own div:
        #   <td class="jv-job-list-location"> Remote<span>,</span>
        #     <div class="jv-meta"> 4 Locations </div></td>
        # The count was surviving into the location column, where "4
        # Locations" reads as a place name that no country logic can parse and
        # no reader can tell from a real one. 65 of the roles in a 12-board
        # sample said this. The real text of the cell is beside it and is what
        # the row actually knows, so the meta div is removed first and the
        # count kept as a flag instead.
        cell = m.group(3)
        cm = _JV_META_COUNT.search(cell)
        place = _text(_JV_META.split(cell, 1)[0]).strip(" ,")
        if _JV_HYBRID.match(place):
            remote: bool | None = False
            place = _JV_HYBRID.sub("", place)
        elif _JV_REMOTE.match(place):
            remote = True
            place = _JV_REMOTE.sub("", place)
        else:
            remote = _remote(place, title)
        # A role whose only stated location was the word "Remote" has to keep
        # saying so. An empty location is read as "no location given", which
        # is a different answer and a different filter branch.
        location = place or ("Remote" if remote else "")

        dept = next((t for pos, t in reversed(heads) if pos < m.start()), "")

        yield Job(
            # Jobvite's list markup never names the employer. LHH's own <h1>
            # is an image whose alt text is "LHH logo", so there is nothing
            # here to check identity against and `discover` will report these
            # boards as agreeing with whatever we already believed.
            company=src.company,
            title=title,
            url=urljoin(src.url, _text(m.group(1))),
            platform="jobvite",
            location=location,
            flags=([f"listed in {cm.group(1)} locations; the list page names "
                    f"none of them"] if cm else []),
            remote=remote,
            department=dept or None,
            # The JSON-LD on the posting page has `datePosted`, but `enrich`
            # only ever writes the description and the pay.
            posted_at=None,
            description="",
            salary=Salary(),
            source_id=src.key,
        )


# --------------------------------------------------------------------------
# JazzHR
# --------------------------------------------------------------------------
# 865 distinct employer hosts on applytojob.com in one Common Crawl index,
# more than any other platform this tool could not read. The board is
# `https://<company>.applytojob.com/apply`, server-rendered, so no browser is
# needed.
#
# Two things worth knowing before touching this.
#
# The RSS feed at `/apply/jobs.rss` answers 410 Gone, so the HTML list is the
# only route. And the whole board arrives on one page: there is no page
# parameter, no offset and no total anywhere in the markup, which is the one
# case where reading a single response is not a truncation bug.
#
# Unusually for this codebase, the page states the employer's own name, in a
# schema.org Organization block. Almost every other adapter fills `company`
# from the Source it was handed, which makes `discover`'s identity check
# circular. Here it can actually be checked.
_JZ_ROW = re.compile(
    r"<li class=[\"']list-group-item[\"']>(.*?)</ul>", re.S | re.I)
_JZ_LINK = re.compile(
    r"<h3[^>]*list-group-item-heading[^>]*>\s*<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
    re.S | re.I)
_JZ_PLACE = re.compile(r"fa-map-marker[^>]*></i>\s*([^<]{1,80})", re.I)
_JZ_DEPT = re.compile(r"fa-sitemap[^>]*></i>\s*([^<]{1,60})", re.I)
_JZ_ORG = re.compile(
    r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)


def _jazzhr_org(text: str) -> str:
    """The employer's own name, from the Organization block on the page."""
    for m in _JZ_ORG.finditer(text or ""):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        for node in (d if isinstance(d, list) else [d]):
            if isinstance(node, dict) and node.get("@type") == "Organization":
                return _text(node.get("name") or "")
    return ""


def parse_jazzhr(payload: Any, src: Source) -> Iterator[Job]:
    """JazzHR, from the server-rendered board at `/apply`."""
    text = payload if isinstance(payload, str) else ""
    org = _jazzhr_org(text)

    for m in _JZ_ROW.finditer(text):
        blk = m.group(1)
        link = _JZ_LINK.search(blk)
        if not link:
            continue
        title = _text(link.group(2))
        if not title:
            continue
        place = _text((_JZ_PLACE.search(blk) or [None, ""])[1]
                      if _JZ_PLACE.search(blk) else "")
        dept = _text((_JZ_DEPT.search(blk).group(1)
                      if _JZ_DEPT.search(blk) else ""))
        remote = _remote(place, title)
        yield Job(
            # The board names itself, so this is the one platform here where
            # the company field is evidence rather than an echo of our label.
            company=org or src.company,
            title=title,
            url=urljoin(src.url, _text(link.group(1))),
            platform="jazzhr",
            location=place or ("Remote" if remote else ""),
            remote=remote,
            department=dept or None,
            # No date, advert text or pay in the list. `enrich` reads the
            # posting page, which carries a JobPosting JSON-LD block.
            posted_at=None,
            description="",
            salary=Salary(),
            source_id=src.key,
        )


# --------------------------------------------------------------------------
# Oracle Taleo
# --------------------------------------------------------------------------
# 255 distinct employer hosts on taleo.net in one Common Crawl index, the
# largest readable gap left after JazzHR. The board is
# `https://<tenant>.taleo.net/careersection/<section>/jobsearch.ftl`, and the
# token is composite (`tenant|section`) because a Taleo tenant runs several
# career sections and none of them is the default: Hilton's is
# `us_hotel_ext`, Transport for London's is `external`, TTEC's is `2`.
#
# Four things cost a session each here.
#
# The page is a JavaScript shell. A plain GET of `jobsearch.ftl` returns no
# job rows at all, so `fetch_taleo` reads the JSON endpoint the page itself
# calls. See the comment there for why the `tz` header is not optional.
#
# The columns are configured per career section and there is no header row in
# the JSON, so nothing may be read by position. Live proof: BAE Systems ship
# ONE column (title only, no location anywhere), Transport for London ship two
# (title, date), TTEC and D.R. Horton ship three (title, locations, date).
# Reading `column[1]` as the location gives BAE nothing and TfL a date. Taleo
# does hand out pointers, `linkedColumn` and `locationsColumns`, and those are
# what this trusts; the date is found by trying to parse the leftovers.
#
# The location is a JSON array serialised INTO the cell, so the raw value is
# the eight characters `["Bath"]` plus the place. It has to be decoded, or
# every location on every Taleo board arrives with brackets and quotes in it.
#
# And Taleo writes a location as a hierarchy joined by hyphens, biggest first:
# `PH-National Capital-Quezon City, Metro Manila`. screen.py's country matcher
# reads comma-separated locations, smallest first, and its US-state rules
# require the comma (`,\s*nebraska`). Handed Taleo's own spelling it resolved
# almost nothing. `_taleo_place` reverses and re-commas, which is the entire
# fix: "Omaha, Nebraska" resolves to US, "Quezon City, Metro Manila, National
# Capital, PH" resolves to PH on the city.
_TL_CELL_SPLIT = 2   # country / region / everything-else, see _taleo_place

# Two-letter codes that may be expanded into a country name, and the list is
# short on purpose. It is exactly the codes screen.py's `_COUNTRY_MARKERS`
# already knows, MINUS every code that is also a US state abbreviation.
# Excluded for that reason and no other: CA (California, not Canada), DE
# (Delaware, not Germany), IN (Indiana, not India), IL (Illinois, not Israel),
# ID (Idaho, not Indonesia), AR (Arkansas, not Argentina). D.R. Horton's board
# is the live proof this matters: it publishes `IN-Indianapolis`,
# `AL-Spanish Fort` and `KY-Louisville` next to `Nebraska-Omaha`, all American,
# and expanding those codes would file them in India, Albania and the Cayman
# Islands. Codes screen.py has never heard of are left alone too, because
# expanding one gains nothing and only invents a place name.
_TL_COUNTRY = {
    "GB": "United Kingdom", "US": "United States", "IE": "Ireland",
    "FR": "France", "ES": "Spain", "NL": "Netherlands", "AU": "Australia",
    "NZ": "New Zealand", "AE": "United Arab Emirates", "SG": "Singapore",
    "HK": "Hong Kong", "JP": "Japan", "CN": "China", "PL": "Poland",
    "PT": "Portugal", "SE": "Sweden", "CH": "Switzerland", "BR": "Brazil",
    "MX": "Mexico", "ZA": "South Africa", "TH": "Thailand", "MY": "Malaysia",
    "PH": "Philippines", "IT": "Italy", "BE": "Belgium", "AT": "Austria",
    "DK": "Denmark", "NO": "Norway", "FI": "Finland", "CZ": "Czechia",
    "RO": "Romania", "TR": "Turkey", "VN": "Vietnam", "KR": "South Korea",
}


def _taleo_place(cell: Any) -> list[str]:
    """One Taleo location cell, rewritten so screen.py can read it.

    Three deliberate rules, each of which cost real roles when it was not
    there.

    It splits at most twice, because Taleo's hierarchy is country, region,
    place and the place itself may be hyphenated. Splitting on every hyphen
    turns `GB-England-Stoke-on-Trent` into five fragments; splitting twice
    keeps the town whole.

    It reverses, so the string reads smallest-first and comma-separated, which
    is the shape screen.py's country matcher was built for. Its US-state rules
    require the comma (`,\\s*nebraska`), so Taleo's own `Nebraska-Omaha`
    matched nothing at all and every D.R. Horton role reached the country
    filter unresolved. Reversed it is "Omaha, Nebraska", which resolves.

    It expands a leading two-letter country code only from `_TL_COUNTRY`,
    which deliberately excludes every code that is also a US state. A bare
    `PH` resolves to nothing (screen.py looks for the word "philippines"), so
    leaving it alone loses TTEC's whole Philippine operation from the country
    facet; expanding `IN` would move D.R. Horton's Indianapolis jobs to India.
    Both of those are on live boards, which is why the answer is a list rather
    than a rule.
    """
    raw = cell if isinstance(cell, str) else _text(cell)
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except ValueError:
        # Not every tenant serialises the cell as an array. A bare string is
        # still a location and must not be thrown away.
        entries = [raw]
    if not isinstance(entries, list):
        entries = [entries]

    out: list[str] = []
    for e in entries:
        s = _text(e)
        if not s:
            continue
        parts = [p.strip() for p in s.split("-", _TL_CELL_SPLIT) if p.strip()]
        # Only the leading segment, which is the one Taleo puts the country
        # in. A two-letter code further down is a region, and "TX" is not
        # Texas-the-country.
        if len(parts) > 1 and parts[0] in _TL_COUNTRY:
            parts[0] = _TL_COUNTRY[parts[0]]
        out.append(", ".join(reversed(parts)))
    return out


def _taleo_date(cells: list[str], used: set[int]) -> str | None:
    """The posting date, found by shape rather than by position.

    There is no header row in the JSON and the columns differ per career
    section, so the only honest way to find the date is to try to parse the
    cells nothing else claimed. Live formats seen: "Aug 24, 2026" (TTEC,
    D.R. Horton) and "13-Aug-26" (Transport for London), which is why `_iso`
    grew the second one.
    """
    for i, c in enumerate(cells):
        if i in used or not isinstance(c, str):
            continue
        got = _iso(c)
        if got:
            return got
    return None


# Taleo publishes no working-arrangement field of any kind: not in the row,
# not in the facets. Remote is stated in the job title when it is stated at
# all ("Data Engineer (Remote)" on TTEC). That means the keyword check is the
# only signal, and it walks straight into the Jobvite trap, where "Hybrid
# Remote" contains the word "remote" and reads as true. A title or location
# that says hybrid is answered False, which is what it is: a hybrid role is
# not open to someone who cannot reach the office.
_TL_HYBRID = re.compile(r"\bhybrid\b", re.I)


def parse_taleo(payload: Any, src: Source) -> Iterator[Job]:
    """Oracle Taleo, from the JSON search endpoint `fetch_taleo` collects.

    The payload is what `fetch_taleo` assembles: every page's rows merged
    under `requisitionList`, plus `employerName` read from the RSS channel
    title, which is the only place on the whole platform where Taleo states
    who the employer is. See `fetch_taleo` for why that is worth a request.

    A career section that does not exist answers **HTTP 200** with
    `careerSectionUnAvailable: true` and every field null, so liveness here is
    the parsed job count and never the status code.
    """
    rows = (payload or {}).get("requisitionList") if isinstance(payload, dict) else None
    employer = _text((payload or {}).get("employerName")) if isinstance(payload, dict) else ""

    for row in rows or []:
        if not isinstance(row, dict):
            continue
        cells = [c if isinstance(c, str) else _text(c)
                 for c in (row.get("column") or [])]
        if not cells:
            continue

        ti = row.get("linkedColumn")
        ti = ti if isinstance(ti, int) and 0 <= ti < len(cells) else 0
        title = _text(cells[ti])
        if not title:
            continue

        loc_idx = [i for i in (row.get("locationsColumns") or [])
                   if isinstance(i, int) and 0 <= i < len(cells)]
        places: list[str] = []
        for i in loc_idx:
            places.extend(_taleo_place(cells[i]))
        # A pipe, because screen.py splits genuinely distinct locations on
        # `[;|/]` and deliberately does not split on a comma: a comma binds a
        # place to the qualifier that identifies its country.
        place = " | ".join(dict.fromkeys(p for p in places if p))

        posted = _taleo_date(cells, {ti, *loc_idx})

        blob = f"{title} {place}"
        remote = False if _TL_HYBRID.search(blob) else _remote(place, title)

        contest = _text(row.get("contestNo") or row.get("jobId"))
        if not contest:
            continue

        yield Job(
            # `employerName` comes from the feed, not from the label we were
            # handed, so identity here is evidence. It is also the ONLY place
            # it is available: both <title> tags on an unbranded Taleo board
            # read "Job Search", which is the shape that collapsed 252 Jobvite
            # employers into one row.
            company=employer or src.company,
            title=title,
            # jobdetail.ftl lives beside jobsearch.ftl in the same career
            # section, and `job=` takes the contest number rather than the
            # internal requisition id.
            url=urljoin(src.url, f"jobdetail.ftl?lang=en&job={contest}"),
            platform="taleo",
            location=place or ("Remote" if remote else ""),
            remote=remote,
            # The row carries no department. Taleo has a JOB_FIELD facet, but
            # it is a summary of the whole board rather than a value per
            # posting, so there is nothing honest to put here.
            department=None,
            posted_at=posted,
            description="",
            # No pay in any of the seven live career sections checked. The
            # advert sometimes states one, and `enrich` re-parses it from
            # there, where the period is written down: a bare figure with no
            # period is the Reed trap, where 650 a day read as 650 a year.
            salary=Salary(),
            source_id=src.key,
        )


# --------------------------------------------------------------------------
# BambooHR
# --------------------------------------------------------------------------
# `/careers/list` is a summary index, not a board. It carries no description,
# no apply URL, no date and no salary, so `enrich` grew a BambooHR fetcher
# that reads `/careers/<id>/detail` for the advert. Without it every one of
# these roles would arrive as a bare title that no dealbreaker and no salary
# floor can be run against.
#
# `locationType` is the field that says how the job is worked, and the field
# actually called `isRemote` is a decoy: it is null on all 155 postings across
# the five live boards checked. The enum was pinned by comparing the JSON
# against the labels BambooHR's own `/jobs/embed2.php` widget renders for the
# same posting ids:
#   "0" -> plain office location   ("Farnborough")        = in-office
#   "1" -> "Remote"                                        = remote
#   "2" -> "(Hybrid)" suffix       ("Farnborough (Hybrid)")= hybrid
# Type 1 is also the only one with no company location at all, which matches
# BambooHR's documented behaviour: picking Remote requires no location.
_BB_OFFICE, _BB_REMOTE, _BB_HYBRID = "0", "1", "2"


def parse_bamboohr(payload: Any, src: Source) -> Iterator[Job]:
    """BambooHR. The board is `https://<company>.bamboohr.com/careers/list`.

    A subdomain that does not exist does NOT 404 here and does not return an
    empty list either. It answers **200 with BambooHR's own marketing
    homepage** as HTML, so both the status code and the content type prove
    nothing and liveness has to be the job count. That is why this tolerates a
    payload that is not a dict at all rather than assuming JSON.

    The list gives no country for office and hybrid roles, only a city and a
    region. See the README: those roles reach the country filter unresolved.
    """
    rows = (payload or {}).get("result") if isinstance(payload, dict) else None
    host = urlparse(src.url).netloc
    for j in rows or []:
        if not isinstance(j, dict):
            continue

        loc_type = str(j.get("locationType") or "")
        office = j.get("location") if isinstance(j.get("location"), dict) else {}
        ats = j.get("atsLocation") if isinstance(j.get("atsLocation"), dict) else {}

        # Remote postings carry no company address, so their only location is
        # the free-text one, which is also the only place a country ever
        # appears in this payload.
        parts = ([_text(ats.get("city")),
                  _text(ats.get("state") or ats.get("province")),
                  _text(ats.get("country"))]
                 if loc_type == _BB_REMOTE or not _text(office.get("city"))
                 else [_text(office.get("city")), _text(office.get("state"))])
        seen: set[str] = set()
        keep: list[str] = []
        for part in parts:
            # "OMAN, OMAN" is a real value on a live board.
            if part and part.lower() not in seen:
                seen.add(part.lower())
                keep.append(part)
        location = ", ".join(keep)

        if loc_type == _BB_REMOTE:
            remote: bool | None = True
        elif loc_type in (_BB_OFFICE, _BB_HYBRID):
            # Never read this off the words. Breezy's own flag was true for
            # hybrid roles and marked an office-based Bournemouth job as
            # remote, which is the one thing a remote filter must never do.
            remote = False
        else:
            remote = _remote(location, j.get("jobOpeningName"))

        jid = _text(j.get("id"))
        if not jid:
            continue

        yield Job(
            # The payload never names the employer, so identity has to come
            # from the source entry and `discover` will report these boards as
            # unchecked rather than falsely ok.
            company=src.company,
            title=_text(j.get("jobOpeningName")),
            # Matches the `jobOpeningShareUrl` the detail endpoint returns,
            # and `enrich` turns it back into the detail URL by appending
            # /detail, so the two have to stay in this shape.
            url=f"https://{host}/careers/{jid}" if host else "",
            platform="bamboohr",
            location=location or ("Remote" if remote else ""),
            remote=remote,
            department=_text(j.get("departmentLabel")) or None,
            # Not in the list payload. `/careers/<id>/detail` has `datePosted`,
            # but `enrich` only ever writes the description and the pay.
            posted_at=None,
            # No advert text here at all. Everything worth screening on is
            # metadata until `enrich` has run.
            description=". ".join(
                x for x in (_text(j.get("employmentStatusLabel")),
                            _text(j.get("departmentLabel"))) if x),
            salary=Salary(),
            source_id=src.key,
        )


# --------------------------------------------------------------------------
# Pinpoint
# --------------------------------------------------------------------------
# The documented public endpoint is `/postings.json`. `/jobs.json` answers too
# but is the deprecated one, and `/api/v1/jobs` is 401 without an X-API-KEY, so
# the free surface is the first of the three and only the first.
#
# What it does not carry is a posting date. There is none in the payload and
# none in the documented schema; the RSS feed at `/jobs.rss` has a <pubDate>
# but carries nothing else useful, so this trades the date for the structured
# pay, location and workplace fields rather than fetching both. Pinpoint roles
# therefore score flat on recency, which is a stated limitation and not a
# parse failure.
_PP_SECTIONS = (
    ("key_responsibilities_header", "key_responsibilities"),
    ("skills_knowledge_expertise_header", "skills_knowledge_expertise"),
    ("benefits_header", "benefits"),
)


def _pinpoint_place(loc: Any) -> str:
    """One Pinpoint location.

    Built from `city` and `province`, deliberately not from `name`, which is
    whatever the employer typed: real values include "Minneapolis, MN" and
    "Anna, IL". A bare two-letter code is the worst possible thing to put in a
    location string, because twenty US state codes are also ISO country codes.
    `province` is spelled out ("California", "New York"), which resolves
    unambiguously.

    Pinpoint publishes no country anywhere in this payload, so the country is
    left for screen.py to infer from the city and state. Inventing one here
    would be guessing.
    """
    if not isinstance(loc, dict):
        return _text(loc)
    city = _text(loc.get("city"))
    province = _text(loc.get("province"))
    parts = [p for p in (city, province) if p]
    # Cities that are their own region give "London, London", which reads as
    # a bug to anyone looking at the dashboard.
    if len(parts) == 2 and parts[0].lower() == parts[1].lower():
        parts.pop()
    return ", ".join(parts) or _text(loc.get("name"))


def parse_pinpoint(payload: Any, src: Source) -> Iterator[Job]:
    """Pinpoint. The board is `https://<company>.pinpointhq.com/postings.json`.

    Like Teamtailor it 404s honestly for a subdomain that does not exist, and
    like every other board here a live one with nothing open answers 200 with
    an empty list, so liveness is the job count.

    `workplace_type` is the field that separates remote from hybrid. Its
    values are `remote`, `hybrid` and `onsite`.

    The advert arrives in four separate fields rather than one, so a parser
    that reads only `description` throws away the responsibilities and the
    must-haves, which is precisely the half the dealbreakers are written
    against.
    """
    rows = (payload or {}).get("data") if isinstance(payload, dict) else None
    for j in rows or []:
        if not isinstance(j, dict):
            continue

        loc = j.get("location") if isinstance(j.get("location"), dict) else {}
        location = _pinpoint_place(loc)

        mode = _text(j.get("workplace_type")).lower()
        if mode == "remote":
            remote: bool | None = True
        elif mode in ("hybrid", "onsite"):
            # Never read this off the advert text. Breezy's own remote flag
            # was true for hybrid roles and marked an office-based Bournemouth
            # job as remote, which is the one thing a remote filter must never
            # do. Pinpoint states it outright, so use the statement.
            remote = False
        else:
            remote = _remote(location, j.get("title"))

        parts = [_text(j.get("description"))]
        for head, body in _PP_SECTIONS:
            txt = _text(j.get(body))
            if txt:
                parts.append(f"{_text(j.get(head))}\n{txt}".strip())
        desc = "\n\n".join(x for x in parts if x)

        sal = from_pinpoint(j)
        if not sal.confirmed:
            # An employer with `compensation_visible` off has not published a
            # figure, but plenty state one in the advert body anyway.
            sal = parse_text(desc[:1500])

        job = j.get("job") if isinstance(j.get("job"), dict) else {}
        dept = job.get("department") if isinstance(job.get("department"), dict) else {}

        yield Job(
            # Pinpoint never names the employer in this payload, not even the
            # hiring organisation, so identity has to come from the source
            # entry. `discover` will report these boards as `unchecked`
            # against a domain rather than falsely `ok`.
            company=src.company,
            title=_text(j.get("title")),
            url=_text(j.get("url")),
            platform="pinpoint",
            location=location or ("Remote" if remote else ""),
            remote=remote,
            department=_text(dept.get("name")) or None,
            # No date exists in this payload. See the note above the parser.
            posted_at=None,
            description=desc,
            salary=sal,
            source_id=src.key,
        )


# --------------------------------------------------------------------------
# Teamtailor
# --------------------------------------------------------------------------
# Two public feeds exist on every career site and they are not equivalent.
# `/jobs.json` is a JSON Feed carrying a schema.org JobPosting per item, but it
# states the country as ISO alpha-2 ("GB") and says nothing at all about
# remote working or department. `/jobs.rss` carries the same descriptions plus
# `<remoteStatus>`, `<tt:department>` and, decisively, `<tt:country>` spelled
# out in full ("United Kingdom"). Reading the RSS is what keeps this adapter
# clear of the country-code trap Breezy walked into, rather than aliasing
# around it afterwards.
#
# The feed defaults to the first 100 jobs and honours `per_page` (verified:
# per_page=2 on a 33-job board returns 2), so the builder asks for 200. A
# board with more than that would silently lose the tail, which is why the
# number is stated here and not left implicit.
_TT_ITEM = re.compile(r"<item>(.*?)</item>", re.S)
_TT_LOCATION = re.compile(r"<tt:location>(.*?)</tt:location>", re.S)


def _tt_tag(block: str, tag: str) -> str:
    m = re.search(rf"<{tag}[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>",
                  block, re.S)
    return _text(m.group(1)) if m else ""


def _tt_place(block: str) -> str:
    """One `<tt:location>`, written the way screen.py reads locations.

    City then country in words, never the alpha-2 code. Two-letter codes are
    actively dangerous here: twenty US state codes are also ISO country codes,
    so "Berlin, DE" resolves to Delaware and "Toronto, CA" to California. The
    full name is unambiguous and is what the country matcher checks first.
    """
    city = _tt_tag(block, "tt:city")
    country = _tt_tag(block, "tt:country")
    name = _tt_tag(block, "tt:name")
    parts = [p for p in (city or name, country) if p]
    # An employer who named the office after the country produces "Latin
    # America, Latin America", which reads as a parse failure to anyone
    # looking at it.
    if len(parts) == 2 and parts[0].lower() == parts[1].lower():
        parts.pop()
    return ", ".join(parts)


def parse_teamtailor(payload: Any, src: Source) -> Iterator[Job]:
    """Teamtailor. The board is `https://<company>.teamtailor.com/jobs.rss`.

    Unlike Ashby, Breezy and SmartRecruiters this one does answer 404 for a
    subdomain that does not exist, so a status code is meaningful. It is still
    not sufficient: a live board with nothing open answers 200 with no items
    (mathem and normative both do), so liveness stays a job count.

    `<remoteStatus>` is the field that separates remote from hybrid. Its
    values are `fully`, `hybrid`, `temporary` and `none`.
    """
    text = payload if isinstance(payload, str) else ""

    # The channel names the employer. `discover` checks a board's identity
    # against its own claim about itself, and falling back to src.company
    # would make every board agree with whatever we already believed.
    head = text.split("<item>", 1)[0]
    board_company = _tt_tag(head, "title")

    for item in _TT_ITEM.findall(text):
        title = _tt_tag(item, "title")
        if not title:
            continue

        names: list[str] = []
        seen: set[str] = set()
        for loc in _TT_LOCATION.findall(item):
            txt = _tt_place(loc)
            if txt and txt.lower() not in seen:
                seen.add(txt.lower())
                names.append(txt)
        # " / " and not ", ": screen.py splits a multi-location string on the
        # slash but reads a comma as binding a place to its qualifier, so a
        # comma fuses "Cambridge, United States" and "Stockholm, Sweden" into
        # one string that resolves to neither.
        location = " / ".join(names)

        status = _tt_tag(item, "remoteStatus").lower()
        if status == "fully":
            remote: bool | None = True
        elif status in ("hybrid", "temporary"):
            # Hybrid is an office job with some days at home, and "temporary"
            # is an office job that is remote for now. Breezy's `is_remote`
            # was true for hybrid roles and marked an office-based Bournemouth
            # job as remote, which is the one thing a remote filter must never
            # do. 14 of 16 roles on Teamtailor's own board are hybrid, so this
            # is the common case here, not an edge case.
            remote = False
        else:
            # `none` is a default as much as a statement, so fall back to
            # reading the words rather than asserting the role is on-site.
            remote = _remote(location, title)

        desc = _tt_tag(item, "description")

        yield Job(
            company=board_company or src.company,
            title=title,
            url=_tt_tag(item, "link"),
            platform="teamtailor",
            location=location or ("Remote" if remote else ""),
            remote=remote,
            department=_tt_tag(item, "tt:department") or None,
            posted_at=_iso(_tt_tag(item, "pubDate")),
            description=desc,
            # No salary field anywhere in the feed, so pay only ever comes
            # from the employer stating it in the advert body.
            salary=parse_text(desc[:1500]),
            # Deliberately not set. Teamtailor names the country in words and
            # screen.py resolves those at its highest tier; a name-to-code
            # table in here would be a second copy of that mapping, and it
            # would have to invent an answer for "Latin America", which
            # Teamtailor really does return as a country.
            source_id=src.key,
        )


def parse_personio(payload: Any, src: Source) -> Iterator[Job]:
    """Personio's `/xml` feed, which states no posting URL at all.

    Every `<position>` carries an id and nothing else to link on, so the URL
    has to be built. It used to be built from `src.company`, which is the
    label a human typed into the source list, not an address: "Auxmoney Gmbh"
    produced `https://Auxmoney Gmbh.jobs.personio.de/job/2727726`, a hostname
    with a space and a capital in it, which is HTTP 400 (checked live,
    2026-08-25). The subdomain that actually serves the board is
    `auxmoney-gmbh`, and the one place it is reliably written down is the URL
    we just fetched, so that is where it now comes from. Three of the four
    boards probed were broken this way; the fourth, Meierhofer, only worked
    because its display name happens to equal its subdomain.
    """
    text = payload if isinstance(payload, str) else ""
    host = urlparse(src.url).netloc
    for block in re.findall(r"<position>(.*?)</position>", text, re.S):
        def g(tag):
            m = re.search(rf"<{tag}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", block, re.S)
            return _text(m.group(1)) if m else ""

        pid = g("id")
        title = g("name")
        if not title:
            continue
        desc = _text(block)
        yield Job(
            company=src.company,
            title=title,
            url=g("url") or (f"https://{host}/job/{pid}" if host and pid else ""),
            platform="personio",
            location=g("office") or g("location"),
            remote=_remote(g("office"), title),
            department=g("department") or None,
            posted_at=_iso(g("createdAt")),
            description=desc,
            salary=parse_text(desc[:1500]),
            # Personio states this outright: "permanent", "intern",
            # "trainee", "freelance", "working_student". Note that its
            # `schedule` tag is the OTHER question (full-time, part-time) and
            # is deliberately not read here.
            employment=from_platform(g("employmentType")),
            source_id=src.key,
        )


# --------------------------------------------------------------------------
# Oracle Recruiting Cloud
# --------------------------------------------------------------------------
_ORC_SITE = re.compile(r"siteNumber=(CX_\d+)", re.I)


def parse_oracle(payload: Any, src: Source) -> Iterator[Job]:
    """Oracle Recruiting Cloud, the system behind a lot of large employers.

    The response nests one level deeper than most: `items[0].requisitionList`
    holds the postings, and `items[0].TotalJobsCount` is the real total rather
    than the page length.

    Two things to know. The host is not derivable from the company name
    (Marks and Spencer sit on `fa-eqid-saasfaprod1.fa.ocs.oraclecloud.com`),
    so these come from reading the careers page. And the list view carries no
    salary at all, only a short description, so roles from here are almost
    always "unconfirmed salary" and that is the platform, not a parse failure.
    """
    items = (payload or {}).get("items") or []
    reqs = []
    for it in items:
        reqs.extend(it.get("requisitionList") or [])

    host = urlparse(src.url).netloc
    m = _ORC_SITE.search(src.url)
    site = m.group(1) if m else "CX_1"

    for j in reqs:
        rid = j.get("Id")
        if not rid:
            continue
        loc = _text(j.get("PrimaryLocation"))
        secondary = ", ".join(
            _text(s.get("Name") or s.get("PrimaryLocation"))
            for s in (j.get("secondaryLocations") or []) if isinstance(s, dict)
        )
        desc = _text(j.get("ShortDescriptionStr") or j.get("ExternalResponsibilitiesStr"))
        yield Job(
            company=src.company,
            title=_text(j.get("Title")),
            url=f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{rid}",
            platform="oracle",
            location=", ".join(x for x in (loc, secondary) if x),
            remote=_remote(loc, j.get("WorkplaceTypeCode"), j.get("Title")),
            department=_text(j.get("JobFamily") or j.get("JobFunction")) or None,
            posted_at=_iso(j.get("PostedDate")),
            description=desc,
            salary=parse_text(desc[:1500]),
            source_id=src.key,
        )


# --------------------------------------------------------------------------
# RSS / Atom (SuccessFactors, Avature, many public-sector boards)
# --------------------------------------------------------------------------
def parse_rss(payload: Any, src: Source) -> Iterator[Job]:
    text = payload if isinstance(payload, str) else ""
    for item in re.findall(r"<(?:item|entry)>(.*?)</(?:item|entry)>", text, re.S):
        def g(tag):
            m = re.search(rf"<{tag}[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", item, re.S)
            return _text(m.group(1)) if m else ""

        title = g("title")
        if not title:
            continue
        link = g("link")
        if not link:
            m = re.search(r'<link[^>]*href="([^"]+)"', item)
            link = m.group(1) if m else ""
        desc = g("description") or g("summary") or g("content")
        yield Job(
            company=src.company,
            title=title,
            url=link,
            platform="rss",
            location=g("location") or "",
            remote=_remote(desc[:300], title),
            posted_at=_iso(g("pubDate") or g("published") or g("updated")),
            description=desc,
            salary=parse_text(desc[:1500]),
            source_id=src.key,
        )


# --------------------------------------------------------------------------
# Generic JSON fallback
# --------------------------------------------------------------------------
_TITLE_KEYS = ("title", "name", "jobTitle", "positionTitle", "text")
_URL_KEYS = ("url", "absolute_url", "jobUrl", "applyUrl", "hostedUrl", "link", "applyLink")


def parse_generic(payload: Any, src: Source) -> Iterator[Job]:
    """Last resort for bespoke boards (Amazon, Netflix, Atlassian and friends).

    Walks the payload for the first list of dicts that look like postings.
    Deliberately conservative: if it cannot find a title and a URL it yields
    nothing rather than inventing a job.
    """
    def walk(node, depth=0):
        if depth > 6:
            return
        if isinstance(node, list):
            if node and isinstance(node[0], dict):
                keys = set(node[0])
                if keys & set(_TITLE_KEYS):
                    yield node
            for x in node[:50]:
                yield from walk(x, depth + 1)
        elif isinstance(node, dict):
            for v in node.values():
                yield from walk(v, depth + 1)

    for candidate in walk(payload):
        for j in candidate:
            if not isinstance(j, dict):
                continue
            title = next((_text(j[k]) for k in _TITLE_KEYS if j.get(k)), "")
            url = next((str(j[k]) for k in _URL_KEYS if j.get(k)), "")
            if not title or not url or not url.startswith("http"):
                continue
            loc = j.get("location") or j.get("locations") or j.get("city") or ""
            if isinstance(loc, dict):
                loc = ", ".join(str(v) for v in loc.values() if isinstance(v, str))
            elif isinstance(loc, list):
                loc = ", ".join(str(x) for x in loc if isinstance(x, str))
            desc = _text(j.get("description") or j.get("content") or "")
            yield Job(
                company=src.company,
                title=title,
                url=url,
                platform=src.platform or "custom",
                location=_text(loc),
                remote=_remote(loc, title),
                # `date_gmt` and `date` come last and they are WordPress's.
                # The commonest bespoke board is a WordPress site exposing its
                # `job` post type at /wp-json/wp/v2/job -- Roke's is one, 34
                # live roles -- and it names its publish date `date`, so every
                # posting from every board of that shape arrived undated and
                # scored as though it had no recency. Last in the chain
                # because the name is generic enough that a board using it for
                # something else should lose to a field that says what it is.
                posted_at=_iso(j.get("postedDate") or j.get("posted_at")
                               or j.get("datePosted") or j.get("publishedAt")
                               or j.get("updated_at") or j.get("created_at")
                               or j.get("date_gmt") or j.get("date")),
                description=desc,
                salary=parse_text(desc[:1500]),
                source_id=src.key,
            )
        return  # only the first plausible list


# --------------------------------------------------------------------------
# NHS Jobs
# --------------------------------------------------------------------------
_NHS_PANEL = re.compile(r'<li class="nhsuk-list-panel search-result.*?(?=<li class="nhsuk-list-panel|</ul>)', re.S)
_NHS_TITLE = re.compile(r'data-test="search-result-job-title"[^>]*>\s*(.*?)\s*</a>', re.S)
_NHS_HREF = re.compile(r'href="(/candidate/jobadvert/[^"?]+)')
_NHS_EMPLOYER = re.compile(
    r'data-test="search-result-location">.*?<h3[^>]*>\s*(.*?)\s*<div class="location-font-size">\s*(.*?)\s*</div>',
    re.S)
_NHS_FIELD = re.compile(
    r'data-test="search-result-{}"[^>]*>.*?<strong[^>]*>\s*(.*?)\s*</strong>', re.S)


def _nhs_field(block: str, name: str) -> str:
    m = re.compile(
        rf'data-test="search-result-{name}"[^>]*>.*?<strong[^>]*>\s*(.*?)\s*</strong>',
        re.S).search(block)
    return _text(m.group(1)) if m else ""


def parse_nhs(payload: Any, src: Source) -> Iterator[Job]:
    """NHS Jobs search results.

    NHS Jobs is the reason a whole sector was invisible: trusts do not use any
    of the commercial applicant tracking systems, so no amount of adding
    employer names reached them. There is a JSON API at /api/v1/search_json but
    it sits behind an auth token, and the .rss path returns HTML rather than a
    feed, so the search page is the route.

    It is worth the parsing. Postings carry Agenda for Change bands, so unlike
    most of the market these roles nearly always state a salary, which means
    the pay filter actually bites here rather than falling through to
    "unconfirmed".
    """
    text = payload if isinstance(payload, str) else ""
    base = "https://www.jobs.nhs.uk"

    for block in _NHS_PANEL.findall(text):
        t = _NHS_TITLE.search(block)
        h = _NHS_HREF.search(block)
        if not (t and h):
            continue

        emp = _NHS_EMPLOYER.search(block)
        employer = _text(emp.group(1)) if emp else "NHS"
        location = _text(emp.group(2)) if emp else ""

        pay = _nhs_field(block, "salary")
        posted = _nhs_field(block, "publicationDate")
        closing = _nhs_field(block, "closingDate")
        jobtype = _nhs_field(block, "jobType")
        pattern = _nhs_field(block, "workingPattern")

        desc = " ".join(x for x in (jobtype, pattern, pay) if x)
        job = Job(
            company=employer,
            title=_text(t.group(1)),
            url=base + h.group(1),
            platform="nhs",
            location=location,
            remote=_remote(location, _text(t.group(1)), pattern),
            department=None,
            posted_at=_iso(posted),
            description=desc,
            salary=parse_text(pay),
            source_id=src.key,
        )
        if closing:
            job.flags.append(f"closes {closing}")
        # The search page carries no duties text, so a dealbreaker scan here
        # would be scanning three metadata fields and calling it clean.
        job.flags.append("not screened: search listing only, open the advert")
        yield job


# --------------------------------------------------------------------------
# Phenom People
# --------------------------------------------------------------------------
_PHENOM_DDO = re.compile(r"phApp\.ddo\s*=\s*(\{.*?\});\s*(?:phApp|</script>|window\.)", re.S)


# Phenom lets each employer type in the apply URL, so some of them have not:
# Aston Carter's board answers with Phenom's own demo placeholder,
# `https://www.ats.com?jobId=123`, on every single posting. That is HTTP 403
# (checked live, 2026-08-25), and because `Job.uid` is keyed on the URL, six
# distinct roles also collapsed into one entry in the seen-set, so five of
# them could never be alerted on at all.
#
# Phenom always serves its own advert page as well, at
# `<board>/job/<jobSeqNo>/<title-slug>`, and that page is the full advert
# rather than an apply form. Verified 200 for Aston Carter, Honda and Advance
# Auto Parts. So it is used whenever the stated apply link cannot be a real
# per-posting address: either it repeats across postings, or it is the known
# placeholder host.
_PH_PLACEHOLDER = re.compile(r"^https?://(?:www\.)?ats\.com\b", re.I)
_PH_SLUG = re.compile(r"[^A-Za-z0-9]+")


def _phenom_url(j: dict, src: Source, counts: dict[str, int]) -> str:
    stated = j.get("applyUrl") or j.get("imApplyUrl") or ""
    usable = bool(stated) and counts.get(stated, 0) < 2 and not _PH_PLACEHOLDER.match(stated)
    if usable:
        return stated
    seq = _text(j.get("jobSeqNo"))
    title = _text(j.get("title"))
    parts = urlparse(src.url)
    # The locale lives in the path of the board URL ("/gb/en/search-results"),
    # and it is not guessable: Serco's is `gb/en` and Thales's `global/en`.
    prefix = parts.path.rsplit("/search-results", 1)[0].strip("/")
    if not (seq and title and parts.netloc):
        return stated
    slug = _PH_SLUG.sub("-", title).strip("-")
    root = f"https://{parts.netloc}" + (f"/{prefix}" if prefix else "")
    return f"{root}/job/{seq}/{slug}"


def parse_phenom(payload: Any, src: Source) -> Iterator[Job]:
    """Phenom renders in the browser, but it also embeds the whole result set
    as JSON in the page under `phApp.ddo`, so there is no need to render
    anything: the jobs are already there in the HTML we fetched.

    Used by large employers who otherwise look unreachable. Serco and Thales
    both sit here. `descriptionTeaser` often carries a salary line even though
    there is no salary field.
    """
    blocks = []
    if isinstance(payload, dict):
        # The /widgets POST API, which pages properly.
        er = payload.get("refineSearch") or payload.get("eagerLoadRefineSearch") or {}
        blocks.append((er.get("data") or {}).get("jobs") or er.get("jobs") or [])
    else:
        text = payload if isinstance(payload, str) else ""
        for m in _PHENOM_DDO.finditer(text):
            try:
                ddo = json.loads(m.group(1))
            except (ValueError, TypeError):
                continue
            er = ddo.get("eagerLoadRefineSearch") or {}
            blocks.append((er.get("data") or {}).get("jobs") or er.get("jobs") or [])
            break

    rows = [j for jobs in blocks for j in jobs if isinstance(j, dict)]
    # An apply link that two postings share is not a per-posting link, so it
    # cannot be the address of either of them. See `_phenom_url`.
    counts: dict[str, int] = {}
    for j in rows:
        u = j.get("applyUrl") or j.get("imApplyUrl") or ""
        if u:
            counts[u] = counts.get(u, 0) + 1

    for j in rows:
        title = _text(j.get("title"))
        url = _phenom_url(j, src, counts)
        if not (title and url):
            continue
        loc = _text(j.get("cityStateCountry") or j.get("location") or j.get("country"))
        teaser = _text(j.get("descriptionTeaser"))
        yield Job(
            company=src.company,
            title=title,
            url=url,
            platform="phenom",
            location=loc,
            remote=_remote(loc, title, j.get("jobType")),
            department=_text(j.get("category")) or None,
            posted_at=_iso(j.get("postedDate") or j.get("dateCreated")),
            description=teaser,
            salary=parse_text(teaser),
            source_id=src.key,
        )


# --------------------------------------------------------------------------
# amazon.jobs (`/en/search.json`)
# --------------------------------------------------------------------------
# Amazon run their own board and their own API. Nothing else here reads it,
# which is why the largest employer on this list was absent from it.
#
# Country codes come back as ISO alpha-3 ("GBR"), which is not what anything
# downstream filters on, and `normalized_location` ends with the same alpha-3
# rather than a country name. So the country is set here from the code rather
# than left for the shared reader to infer from "Cambridge, England, GBR".
_A3 = {
    "GBR": "UK", "USA": "US", "CAN": "CA", "IRL": "IE", "DEU": "DE",
    "FRA": "FR", "ESP": "ES", "ITA": "IT", "NLD": "NL", "POL": "PL",
    "IND": "IN", "AUS": "AU", "NZL": "NZ", "JPN": "JP", "SGP": "SG",
    "ARE": "AE", "ZAF": "ZA", "BRA": "BR", "MEX": "MX", "SWE": "SE",
    "CHE": "CH", "AUT": "AT", "BEL": "BE", "DNK": "DK", "FIN": "FI",
    "NOR": "NO", "PRT": "PT", "CZE": "CZ", "ROU": "RO", "TUR": "TR",
    "ISR": "IL", "SAU": "SA", "EGY": "EG", "KOR": "KR", "CHN": "CN",
    "MYS": "MY", "PHL": "PH", "IDN": "ID", "THA": "TH", "VNM": "VN",
    "LUX": "LU", "GRC": "GR", "HUN": "HU", "SVK": "SK", "BGR": "BG",
    "HRV": "HR", "SVN": "SI", "EST": "EE", "LVA": "LV", "LTU": "LT",
    "COL": "CO", "CHL": "CL", "ARG": "AR", "PER": "PE", "CRI": "CR",
    "JOR": "JO", "NGA": "NG", "KEN": "KE", "TWN": "TW", "HKG": "HK",
}


def _strip_tags(v: Any) -> str:
    """Advert text with the markup taken out and the line breaks kept.

    `_text` collapses all whitespace, which turns a qualifications list into
    one unreadable paragraph and loses the bullet structure a reader and a
    dealbreaker regex both use. `<br/>` and `</li>` become newlines first.
    """
    if not isinstance(v, str) or not v:
        return ""
    s = re.sub(r"<br\s*/?>|</li>|</p>|</div>", "\n", v, flags=re.I)
    s = _TAGS.sub("", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def parse_amazon(payload: Any, src: Source) -> Iterator[Job]:
    """Rows from `jobs`.

    The advert is spread over three fields and all three matter. `description`
    alone is the pitch; `basic_qualifications` is where the must-haves live,
    which is what dealbreakers and fit are actually judged on, and it is the
    field that says "5+ years" or "prior experience as a software engineer".
    Joining them is the difference between screening the advert and screening
    the marketing.

    `company_name` is the legal entity that employs you, and it varies by
    country and business: "Amazon Web Services Malaysia SDN. BHD.",
    "Amazon France Logistique SAS". Stored as the source's name instead, so a
    board does not fragment into two hundred employers on the dashboard, with
    the entity kept in the advert text where it belongs.
    """
    rows = (payload or {}).get("jobs") if isinstance(payload, dict) else None
    for j in rows or []:
        if not isinstance(j, dict):
            continue
        title = _text(j.get("title"))
        path = _text(j.get("job_path"))
        if not (title and path):
            continue
        parts = [_strip_tags(j.get(k)) for k in
                 ("description", "basic_qualifications", "preferred_qualifications")]
        desc = "\n\n".join(p for p in parts if p)
        code = (j.get("country_code") or "").upper()
        # "Virtual" is Amazon's word for a home-based role. It is not a town,
        # and stored as one it lands in the dashboard's city filter looking
        # exactly like a place you could commute to. Same failure as Workday's
        # "2 Locations", in a different costume.
        city = _text(j.get("city"))
        virtual = city.lower() in ("virtual", "remote")
        if virtual:
            city = ""
        loc = _text(j.get("normalized_location"))
        # Drop the trailing alpha-3. It is already carried as `country`, and
        # left in it the city filter lists "GBR" as a town.
        if code and loc.upper().endswith(code):
            loc = loc[: -len(code)].rstrip(" ,")
        if not loc:
            # `normalized_location` is bare "GBR" on virtual roles and on some
            # ordinary ones, so stripping the code empties it. Two of the
            # first three hundred rows read this way. Rebuild from the parts,
            # and fall back to `location`, which is "GB, East London": the
            # leading alpha-2 goes for the same reason the alpha-3 did.
            loc = ", ".join(x for x in (city, _text(j.get("state"))) if x)
        if not loc:
            raw = _text(j.get("location"))
            bits = [b.strip() for b in raw.split(",")]
            if bits and len(bits[0]) == 2 and bits[0].isupper():
                bits = bits[1:]
            loc = ", ".join(b for b in bits if b and b.lower() != "virtual")
        yield Job(
            company=src.company,
            title=title,
            url=urljoin("https://www.amazon.jobs/", path),
            platform="amazon",
            location=loc,
            city=city,
            country=_A3.get(code) or None,
            # The row says so outright, which beats reading the words in a
            # title. `type: VIRTUAL` in `locations` says the same thing.
            remote=True if virtual else _remote(loc, title),
            department=_text(j.get("job_category")) or None,
            posted_at=_iso(_text(j.get("posted_date"))),
            description=desc,
            source_id=src.key,
        )



# --------------------------------------------------------------------------
# Phenom PCSX (`/api/pcsx/search`)
# --------------------------------------------------------------------------
# Phenom's newer product, and a different system from `parse_phenom` above
# despite the shared vendor. That one reads `phApp.ddo` out of rendered HTML
# or POSTs to `/widgets`; this one is a plain GET returning JSON under
# `data.positions`, and neither adapter can read the other's board.
#
# Microsoft sit here, which is why they were absent from a 17,811-source list:
# they are on none of the platforms this tool could read, and `discover` found
# nothing because there was nothing to find.
#
# The list carries no advert text at all, only a title, places, a department
# and a timestamp. `enrich._from_pcsx` fetches the real one per role. That is
# deliberate rather than a gap: a role stored with no description is marked
# unscreened, which is the honest state, and inventing a teaser from the title
# would let the dealbreakers run against nothing and report that they passed.
_PCSX_JOB = re.compile(r"/careers/job/(\d+)")


def parse_pcsx(payload: Any, src: Source) -> Iterator[Job]:
    """Rows from `data.positions`.

    `locations` is a list of free-text strings, most specific last, in the
    shape "United States, Washington, Redmond". A posting open in several
    places carries several of them, so they are joined rather than reduced to
    a count: Workday and Jobvite both collapse that to the string
    "2 Locations", which then sits in the location column reading exactly like
    a city, and this adapter is not repeating it.
    """
    data = (payload or {}).get("data") if isinstance(payload, dict) else None
    for j in (data or {}).get("positions") or []:
        if not isinstance(j, dict):
            continue
        title = _text(j.get("name"))
        rel = _text(j.get("positionUrl"))
        jid = j.get("id")
        # The row's own address, never a search URL: `positionUrl` is relative
        # and the id is the only other thing that identifies the posting, so a
        # row carrying neither is a row this tool cannot link to.
        if rel:
            url = urljoin(src.url, rel)
        elif jid:
            url = urljoin(src.url, f"/careers/job/{jid}")
        else:
            continue
        if not title:
            continue
        places = [p for p in (j.get("locations") or []) if isinstance(p, str)]
        if not places:
            places = [p for p in (j.get("standardizedLocations") or [])
                      if isinstance(p, str)]
        # Microsoft write "United States, Multiple Locations, Multiple
        # Locations" for a posting open in several places. "Multiple
        # Locations" is a count wearing a place's clothes, and left in it
        # becomes the city on the dashboard. The country in front of it is
        # real and is kept.
        # PCSX writes a place most-general-first: "United States, Washington,
        # Redmond". Every other board here writes it the other way round, and
        # `screen.city_of` takes the first comma part, so left alone every
        # Microsoft role's city was its country and 2,119 of them had none at
        # all. Reversed here, in the adapter, because normalising into `Job`
        # is this layer's job and changing the shared reader to guess the
        # ordering would touch every platform.
        #
        # "Multiple Locations" is dropped on the way through: it is a count
        # wearing a place's clothes, exactly like Workday's "2 Locations", and
        # left in it becomes the city on the dashboard.
        tidy = []
        for place in places:
            parts = [part.strip() for part in place.split(",")]
            parts = [part for part in parts
                     if part and part.lower() != "multiple locations"]
            if parts:
                tidy.append(", ".join(reversed(parts)))
        loc = "; ".join(dict.fromkeys(tidy))
        # `workLocationOption` is the employer's own answer, so it beats
        # guessing from the words in a title.
        mode = _text(j.get("workLocationOption")).lower()
        remote = True if mode == "remote" else (False if mode else None)
        if remote is None:
            remote = _remote(loc, title)
        yield Job(
            company=src.company,
            title=title,
            url=url,
            platform="pcsx",
            location=loc,
            remote=remote,
            department=_text(j.get("department")) or None,
            posted_at=_iso(j.get("postedTs") or j.get("creationTs")),
            # No advert in the list. Left empty on purpose: see above.
            description="",
            source_id=src.key,
        )


# --------------------------------------------------------------------------
# Google Careers (`AF_initDataCallback`, key `ds:1`)
# --------------------------------------------------------------------------
# Google publish no ATS feed. The careers site is server-rendered and the
# whole result set is already in the HTML, inside the `ds:1` boot payload the
# page hands to its own JavaScript, so this reads structured data rather than
# scraping rendered markup. Nothing is executed and no private endpoint is
# called: it is the same bytes any reader gets.
#
# The rows are positional, not keyed, which is the risk here. A keyed API that
# renames a field gives you an empty string; a positional one that gains a
# column at index 3 gives you the wrong field's contents, confidently. So the
# indices below are asserted by shape, and a row whose title or id does not
# look like a title or an id is dropped rather than stored as something else.
_G_FIELDS = {
    "id": 0, "title": 1, "apply": 2, "responsibilities": 3,
    "qualifications": 4, "company": 7, "locations": 9, "about": 10,
    "posted": 12, "min_quals": 19,
}


def _g_html(row: list, i: int) -> str:
    """One of the `[null, "<html>"]` pairs the payload wraps its prose in."""
    if i >= len(row):
        return ""
    cell = row[i]
    if isinstance(cell, list) and len(cell) > 1:
        return _strip_tags(cell[1])
    return _strip_tags(cell) if isinstance(cell, str) else ""


def parse_google_careers(payload: Any, src: Source) -> Iterator[Job]:
    """Rows from the `ds:1` boot payload.

    The advert is spread across four fields and all four matter: the
    responsibilities, the "about the job" prose, and BOTH qualification
    blocks, because Google put the minimum bar in one and the preferred bar
    in another. A dealbreaker regex reading only one of them is reading half
    the advert, which is the shape this repo keeps producing.

    `locations` is a list of rows, each `[display, [address], city, postcode,
    region, countryCode]`, and a posting open in several places carries
    several. They are joined, never counted: "3 Locations" written into the
    location column sits exactly where a city would and reads as one.
    """
    rows = payload[0] if isinstance(payload, list) and payload else []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, list) or len(row) <= _G_FIELDS["locations"]:
            continue
        jid = row[_G_FIELDS["id"]]
        title = _text(row[_G_FIELDS["title"]])
        # Positional rows: an id that is not a digit string, or a title that
        # is a URL, means the columns have moved under us. Dropping the row
        # is right; storing it would put a URL in the title column, where it
        # would render as a job nobody can tell is broken.
        if not title or not isinstance(jid, str) or not jid.isdigit():
            continue
        if title.startswith("http"):
            continue

        places = []
        for place in (row[_G_FIELDS["locations"]] or []):
            if isinstance(place, list) and place and isinstance(place[0], str):
                places.append(place[0].strip())
            elif isinstance(place, str):
                places.append(place.strip())
        loc = "; ".join(dict.fromkeys(p for p in places if p))

        # The country code the payload states, rather than one guessed from
        # the words in the location string. "London, UK" and "London, ON,
        # Canada" both start "London".
        country = None
        first = (row[_G_FIELDS["locations"]] or [None])[0]
        if isinstance(first, list) and len(first) > 5 and isinstance(first[5], str):
            country = first[5].strip().upper() or None

        # The row carries a sign-in URL with a one-shot token in it. That is
        # not an address for a posting: it expires, and it is keyed to
        # whoever fetched it. The canonical page is built from the id.
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        url = ("https://www.google.com/about/careers/applications"
               f"/jobs/results/{jid}-{slug}")

        description = "\n\n".join(part for part in (
            _g_html(row, _G_FIELDS["about"]),
            _g_html(row, _G_FIELDS["responsibilities"]),
            _g_html(row, _G_FIELDS["min_quals"]),
            _g_html(row, _G_FIELDS["qualifications"]),
        ) if part)

        # `[seconds, nanos]`, and `_iso` reads the seconds. A bare list would
        # be read as a millisecond epoch and land in 1970.
        posted = row[_G_FIELDS["posted"]] if len(row) > _G_FIELDS["posted"] else None
        if isinstance(posted, list) and posted and isinstance(posted[0], (int, float)):
            posted = posted[0]
        else:
            posted = None

        # Alphabet companies post to this same board under their own name:
        # DeepMind, Waymo, GFiber, Verily, Wing, YouTube. Using the row's
        # company rather than the source's keeps "DeepMind" on a DeepMind
        # role instead of relabelling all of them "Google".
        company = _text(row[_G_FIELDS["company"]]) or src.company

        yield Job(
            company=company,
            title=title,
            url=url,
            platform="google_careers",
            location=loc,
            remote=_remote(loc, title, description),
            posted_at=_iso(posted),
            description=description,
            country=country or src.country,
            sector=src.sector,
            source_id=src.key,
        )


# --------------------------------------------------------------------------
# SuccessFactors RMK (jobs2web)
# --------------------------------------------------------------------------
_RMK_LINK = re.compile(
    r'<a[^>]*class="[^"]*jobTitle-link[^"]*"[^>]*href="([^"?]+)"[^>]*>\s*(.*?)\s*</a>', re.S)
_RMK_ANY = re.compile(r'href="((?:/[a-z0-9_-]+)?/job/[^"?]+)"[^>]*>\s*(.*?)\s*</a>', re.S | re.I)
# One result row. The table is the whole board, so splitting on it is what
# keeps a row's location and date attached to that row's title rather than to
# whichever one happened to be nearest in the document.
_RMK_ROW = re.compile(r'<tr[^>]+class="[^"]*data-row[^"]*"[^>]*>(.*?)</tr>', re.S | re.I)
# The location cell, not any `jobLocation` span: the same span is repeated
# inside the title cell for the phone layout, and matching that one first
# would work by luck rather than by rule.
_RMK_LOC = re.compile(
    r'<td[^>]+class="[^"]*colLocation[^"]*"[^>]*>(.*?)</td>', re.S | re.I)
_RMK_DATE = re.compile(
    r'<td[^>]+class="[^"]*colDate[^"]*"[^>]*>(.*?)</td>', re.S | re.I)
_ALNUM = re.compile(r"[^a-z0-9]+")


def _rmk_slug_location(path: str, title: str) -> str:
    """Where the row says, read out of the href when the page has no location
    column.

    The slug is `<place>-<title>` and the old rule was to find the title in it
    verbatim and keep what came before. That rule fails whenever the title
    contains a character the slug drops, which is most of them: adidas's
    "ALTERNANCE - Vendeur Polyvalent adidas (H/F/D)" is slugged
    "ALTERNANCE-Vendeur-Polyvalent-adidas-(HFD)", so `title in slug` was False
    and the location became the entire slug, title included. Live proof before
    this changed: adidas reported a location of "Ile Saint Denis ALTERNANCE
    Vendeur Polyvalent adidas (HFD)" and Scotiabank one of "Toronto Senior
    Manager, Global Connectivity, International Wealth Management Toronto, ON
    ON M5H 0B4". Neither is a place, and a location filter can do nothing with
    either.

    So both sides are compared with punctuation removed, and the match is
    mapped back to an index in the original. A title that still cannot be
    found means the slug's shape is not the one assumed, and then nothing is
    returned: no location at all is honest, whereas the whole slug is page
    furniture wearing a location's clothes.
    """
    slug = path.rsplit("/job/", 1)[-1]
    slug = re.sub(r"/\d+/?$", "", slug)
    slug = _text(unquote(slug).replace("-", " "))
    # Twice, because some tenants escape the href twice: Burberry serve
    # `Women&amp;apos;s`, which one pass leaves as `&apos;` and the comparison
    # below then reads as the four letters "apos", so the title stops matching
    # its own slug and the location is lost.
    for _ in range(2):
        if "&" not in slug:
            break
        slug = html.unescape(slug)
    # Index of each kept character in the original, so a hit in the
    # punctuation-free copy can be turned back into a cut point.
    keep, back = [], []
    for i, ch in enumerate(slug.lower()):
        if ch.isalnum():
            keep.append(ch)
            back.append(i)
    flat_slug = "".join(keep)
    flat_title = _ALNUM.sub("", title.lower())
    if not flat_title or flat_title not in flat_slug:
        return ""
    return slug[: back[flat_slug.rindex(flat_title)]].strip(" ,-")


def parse_rmk(payload: Any, src: Source) -> Iterator[Job]:
    """SAP SuccessFactors Recruiting Marketing, still served from jobs2web
    hostnames. Server-rendered, so it parses without a browser.

    Transport for London sit here, and so do many public bodies that look like
    they have no machine-readable board at all. The href carries a tenant
    prefix (`/tfl/job/...`) rather than a bare `/job/`.

    The result table has its own location and date columns and they were both
    being thrown away: location was being reconstructed out of the href slug,
    badly (see `_rmk_slug_location`), and the date was not read at all, so
    every posting from all 93 boards on this platform arrived undated and
    scored as though it had no recency. Both are plain text in the row --
    `<td class="colLocation"><span class="jobLocation">Ile Saint-Denis, FR` and
    `<td class="colDate"><span class="jobDate">Aug 25, 2026` -- so both are now
    read from there, and the slug is only consulted when a tenant has switched
    the location column off. Which they do: the columns are configured per
    tenant, the same way Taleo's are.
    """
    text = payload if isinstance(payload, str) else ""
    base = f"https://{urlparse(src.url).netloc}"

    rows = [(m.group(1)) for m in _RMK_ROW.finditer(text)]
    # A tenant whose markup this does not recognise still gets the old
    # whole-document scan rather than an empty board.
    pairs: list[tuple[str, str, str, str]] = []
    for row in rows:
        link = _RMK_LINK.search(row) or _RMK_ANY.search(row)
        if not link:
            continue
        lo = _RMK_LOC.search(row)
        da = _RMK_DATE.search(row)
        pairs.append((link.group(1), link.group(2),
                      _text(lo.group(1)) if lo else "",
                      _text(da.group(1)) if da else ""))
    if not pairs:
        pairs = [(p, t, "", "")
                 for p, t in (_RMK_LINK.findall(text) or _RMK_ANY.findall(text))]

    seen = set()
    for path, raw_title, place, date in pairs:
        title = _text(raw_title)
        if not title or path in seen:
            continue
        seen.add(path)
        # "/tfl/job/Palestra-House,-Southwark,-SE1-Assistant-Safety-Manager/1349"
        loc = place or _rmk_slug_location(path, title)
        yield Job(
            company=src.company,
            title=title,
            url=path if path.startswith("http") else base + html.unescape(path),
            platform="rmk",
            location=loc,
            remote=_remote(loc, title),
            posted_at=_iso(date),
            description="",
            salary=Salary(),
            source_id=src.key,
            flags=["not screened: search listing only, open the advert"],
        )


# --------------------------------------------------------------------------
# Avature
# --------------------------------------------------------------------------
# Avature serves absolute hrefs, not paths.
#
# `[^"?]` and not `[^"]` before /JobDetail/: every card also carries Twitter
# and Facebook share links whose QUERY STRING contains the job's own URL
# (`?text=<title> https://.../JobDetail/...`). Metro Bank's six roles come with
# twelve such links. They only fail to parse today because the anchor wraps an
# icon rather than text and the empty title is dropped, which is luck rather
# than a rule: the moment one carries a label the board reports three rows per
# job.
# Two record types, not one. A pipeline is Avature's evergreen requisition and
# it is a real vacancy: HSBC's board carries 96 PipelineDetail links and zero
# JobDetail ones, so reading only the latter reported it as empty. Six large
# boards were invisible this way, 1,028 postings between them (Macquarie 563,
# Coca-Cola HBC 351, HSBC 48). Those boards also take `pipelineRecordsPerPage`
# rather than `jobRecordsPerPage`, so the page size has to match the kind.
# Two href shapes as well as two record types. Tesco and Metro Bank serve
# `/JobDetail/Some-Slug`; Macquarie and Ross Stores serve
# `/JobDetail?jobId=23921` with the id in the query and no slug at all.
# The `[^"?]` prefix guard stays: every card also carries share links whose
# QUERY STRING contains the job's own URL (`?text=<title> https://.../JobDetail/...`),
# and allowing a question mark anywhere before the match reports three rows
# per job. So the query form is admitted only as exactly `?jobId=<digits>`,
# which a share link never is.
_AV_LINK = re.compile(
    r'href="(https?://[^"?]*?/(?:Job|Pipeline)Detail'
    r'(?:/[^"?]+|\?jobId=\d+))"[^>]*>\s*(.*?)\s*</a>',
    re.S)
_AV_KIND = re.compile(r"/((?:Job|Pipeline)Detail)/")
# A card links the same record twice, once on the title and once on a "View
# Job" button. Keeping the first would be luck; the labelled one is the title.
_AV_NOT_A_TITLE = re.compile(
    r"^(?:view|apply|details?|read more|learn more|see)\b", re.I)
# The card's subtitle strip. Avature names each field in the class, so these
# are read by name and never by position: which of them a board emits, and in
# what order, is the tenant's choice.
_AV_LOC = re.compile(r'list-item-location[^>]*>\s*(.*?)\s*</span>', re.S | re.I)
_AV_POSTED = re.compile(r'list-item-posted[^>]*>\s*(.*?)\s*</span>', re.S | re.I)
_AV_WORK = re.compile(r'list-item-workplace[^>]*>\s*(.*?)\s*</span>', re.S | re.I)

# A third Avature template, and the one that kept 65% of Avature roles
# placeless. Frequentis, the University of Colorado and Nva emit no
# `list-item-location` column at all. The place is still on the card, in an
# unlabelled pipe-separated subtitle:
#
#   Public Safety & Transport Offer Management | Österreich | Wien | FREQUENTIS AG
#
# Nothing says which segment is the location, and the order is not the same
# on every board, so reading by position would be a guess dressed up as a
# parse. Instead each segment is offered to the country logic this tool
# already uses to place a role, and the ones it recognises are kept. That
# logic knows "Österreich" is Austria and "Wien" is in it, so this needs no
# new place data and inherits every future improvement to it.
_AV_SUBTITLE = re.compile(
    r'(?:list__item__text|article__header__text)__subtitle[^>]*>(.*?)</div>',
    re.S | re.I)

# Avature boards that use the subtitle strip label the place three ways:
#
#   Frequentis  ... Offer Management | Österreich | Wien | FREQUENTIS AG
#   Lenovo      <span>United States of America, North Carolina, Whitsett</span>
#   Xerox       <p><span>City:</span> Webster</p><p><span>State:</span> NY</p>
#
# so a label is read when there is one and the country logic is asked when
# there is not. Anything else on the strip -- a requisition number, a
# department, the legal entity -- fails both and is dropped.
_AV_FIELD = re.compile(
    r'^(?:city|state(?:/province)?|province|country|location|region)\s*:\s*'
    r'(.+)$', re.I)


def _av_subtitle_place(blk: str, company: str) -> str:
    """The location parts of an unlabelled subtitle strip, in reading order.

    Nothing on these strips says which part is the location, and the order is
    not the same on any two boards, so reading by position would be a guess
    dressed up as a parse. Each part is offered to the country logic this tool
    already uses to place a role, and the parts it recognises are kept. That
    logic knows "Österreich" is Austria and "Wien" is in it, so this needs no
    new place data and inherits every future improvement to it.

    A labelled part ("City: Webster") is taken on its label instead, because
    the label is better evidence than a lookup and some city names are also
    surnames, departments or products.

    The employer's own name is dropped explicitly, since a company named after
    a city would otherwise be read as one.

    Country names sort last so the result reads "Wien, Österreich" rather than
    the other way round, which is the order the rest of the tool writes a
    location in. The sort is stable, so "Webster, New York" keeps the order
    its own labels gave it.
    """
    from ..screen import _country_of, names_a_country
    m = _AV_SUBTITLE.search(blk)
    if not m:
        return ""
    # One candidate per <p> or <br>, plus pipe-separated parts within them, so
    # all three shapes above reduce to the same list of strings. Deliberately
    # NOT split on <span>: Xerox puts the label in one and the value beside
    # it, so splitting there separated "City:" from "Webster" and threw the
    # city away while keeping the state.
    chunks = re.split(r"</?(?:p|br)[^>]*>", m.group(1))
    parts = [_text(x) for chunk in chunks for x in chunk.split("|")]
    keep = []
    for seg in parts:
        if not seg or seg.casefold() == (company or "").casefold():
            continue
        f = _AV_FIELD.match(seg)
        if f:
            val = _text(f.group(1))
            if val:
                keep.append(val)
        elif _country_of(seg):
            keep.append(seg)
    keep.sort(key=names_a_country)
    return ", ".join(dict.fromkeys(keep))


def parse_avature(payload: Any, src: Source) -> Iterator[Job]:
    """Avature's hosted careers site. Server-rendered links to /JobDetail/.

    The card under each link carries a subtitle strip of labelled spans, and
    two of them were being thrown away. `list-item-location` holds a real
    place -- "Richmond, VIC" for Australia Post, "Barcelona, Chicago, Madrid,
    Mexico City, Sao Paulo" for Baker McKenzie -- and this parser used to hard
    code `location=""` for every Avature posting on the grounds that the
    location was only ever in the slug. It is in the markup on the boards that
    emit the column, and empty is what a location filter sees when a role has
    no location at all, so those roles were being judged as placeless.
    `list-item-posted` holds "Posted 13-Aug-2026", and dropping it left every
    posting from all 95 Avature boards undated and unable to score for
    recency.

    Both spans are optional -- Bravura's board emits neither -- so both fall
    back to nothing rather than to a guess. The strip is read from the window
    that follows the title link and is cut at the next card, so a board where
    one row omits the location cannot borrow the next row's.
    """
    text = payload if isinstance(payload, str) else ""
    best: dict[str, str] = {}
    order: list[str] = []
    card: dict[str, str] = {}
    for m in _AV_LINK.finditer(text):
        title = _text(m.group(2))
        if not title or _AV_NOT_A_TITLE.match(title):
            continue
        url = m.group(1)
        if url not in best:
            order.append(url)
            # Wide enough to reach the end of the subtitle strip through the
            # markup's indentation -- Australia Post's `list-item-posted` sits
            # 2,596 characters past its title link -- and cut at the next card
            # regardless, so one row can never borrow the next row's fields.
            window = text[m.end():m.end() + 9000]
            edge = window.find("article--result")
            card[url] = window[:edge] if edge > 0 else window
        # Longest wins: a card sometimes labels the same link twice and the
        # fuller one is the job title rather than a truncated repeat.
        if len(title) > len(best.get(url, "")):
            best[url] = title
    for url in order:
        title = best[url]
        # The slug carries the location when the markup does not.
        m = _AV_KIND.search(url)
        kind = m.group(1) if m else "JobDetail"
        tail = url.rsplit(f"/{kind}", 1)[-1]
        # The slug carries the location on the path form. The `?jobId=` form
        # carries nothing, so there is no pseudo-description to invent.
        slug = "" if tail.startswith("?") else tail.lstrip("/").replace("-", " ")
        blk = card.get(url, "")
        lm, pm, wm = (_AV_LOC.search(blk), _AV_POSTED.search(blk),
                      _AV_WORK.search(blk))
        loc = _text(lm.group(1)) if lm else ""
        if not loc:
            loc = _av_subtitle_place(blk, src.company)
        # "Posted 13-Aug-2026" -- the label is part of the span's text.
        posted = re.sub(r"^posted\s+", "", _text(pm.group(1)), flags=re.I) if pm else ""
        yield Job(
            company=src.company,
            title=title,
            url=url,
            platform="avature",
            location=loc,
            remote=_remote(loc, _text(wm.group(1)) if wm else "", slug, title),
            posted_at=_iso(posted),
            description=_text(slug),
            salary=Salary(),
            source_id=src.key,
            flags=["not screened: search listing only, open the advert"],
        )


# --------------------------------------------------------------------------
# iCIMS
# --------------------------------------------------------------------------
_ICIMS_ITEM = re.compile(r'<div class="row">(.*?)(?=<div class="row">|</body>)', re.S)
_ICIMS_LINK = re.compile(
    r'<a[^>]+href="(https?://[^"]*?/jobs/\d+/[^"?]+[^"]*)"[^>]*class="iCIMS_Anchor"[^>]*>(.*?)</a>',
    re.S)
# iCIMS renders the location two different ways, and this matched one of them.
#
#   <span class="field-label">Job Locations</span> <span>UK-London</span>
#   <span class="sr-only field-label">Location : Location</span> </dt>
#     <dd class="iCIMS_JobHeaderData"><span> US-AZ-Chandler</span></dd>
#
# The second is a screen-reader label followed by a definition-list value, and
# on a sample of 258 boards 55% of iCIMS roles came back with no location
# because of it. iCIMS is 1,744 of the bundled boards, the second largest
# platform in the list, and a role with no location cannot be filtered by
# country.
#
# So the label is matched on the word rather than on one exact phrase, and the
# value is taken from whichever of `<span>` or `<dd>` comes next. `[^<]*` on
# the label keeps it from running past its own tag into unrelated markup.
_ICIMS_LOC = re.compile(
    r'field-label"[^>]*>[^<]*?Locations?[^<]*</span>'      # either label
    r'(?:\s*</dt>)?\s*'                                    # the dl variant
    r'(?:<dd[^>]*>)?\s*<span[^>]*>\s*(.*?)\s*</span>', re.S)

# iCIMS states the page's verdict in one div and only when there is a verdict
# to state. `iCIMS_GenericMessage` is that div. The other two error boxes on
# these pages -- `iCIMS_NoCookies` and the geolocation one -- are boilerplate
# and sit on healthy boards too: 192 of 200 boards with live postings carry
# the cookie box, so anything keying on `iCIMS_ErrorMessage` alone would call
# nearly every board broken.
_ICIMS_VERDICT = re.compile(
    r'class="[^"]*iCIMS_GenericMessage[^"]*"[^>]*>(.*?)</div>', re.S)

# The one verdict that really does mean "this board has nothing today".
# Everything else in that div is the board declining to show us the listing,
# which is not the same fact and must not be recorded as one. Measured on
# 2026-08-26 across 949 live iCIMS boards: four said this, and
# referral-publicisgroupe.icims.com answered HTTP 200 with "Error: Login is
# required to search for jobs." Both parsed to zero postings, both were
# reported as a board with no vacancies, and `validate --prune` deletes a
# board it reads as dead.
_ICIMS_REALLY_EMPTY = re.compile(
    r"no jobs were found|no results were found|no matching", re.I)


def parse_icims(payload: Any, src: Source) -> Iterator[Job]:
    """iCIMS renders its results into an iframe, so the plain search page comes
    back as a shell with no jobs in it. Adding `in_iframe=1` returns the
    server-rendered list instead, which is the whole trick.

    Locations arrive pipe-separated in a single span ("UK-London |
    UK-Wolverhampton"), and the leading country code makes them read oddly, so
    they are tidied here rather than left to confuse the location filter.
    """
    text = payload if isinstance(payload, str) else ""
    seen = set()

    rows = _ICIMS_ITEM.findall(text)
    if not rows:
        verdict = _ICIMS_VERDICT.search(text)
        said = _text(verdict.group(1)) if verdict else ""
        if said and not _ICIMS_REALLY_EMPTY.search(said):
            # Not an empty board. The board answered and refused, and the
            # difference is the whole point of this exception existing.
            raise BoardUnreadable(
                f"iCIMS answered HTTP 200 and said {said[:160]!r} instead of "
                f"returning a listing, so this is not a board with no "
                f"vacancies")

    for block in rows:
        lm = _ICIMS_LINK.search(block)
        if not lm:
            continue
        # The anchor wraps a screen-reader label ("Title") before the heading,
        # which _text would otherwise fold into the job title.
        inner = re.sub(r'<span[^>]*class="[^"]*sr-only[^"]*"[^>]*>.*?</span>', " ",
                       lm.group(2), flags=re.S)
        url, title = lm.group(1), _text(inner)
        if not title or url in seen:
            continue
        seen.add(url)

        loc = ""
        lo = _ICIMS_LOC.search(block)
        if lo:
            parts = [p.strip() for p in _text(lo.group(1)).split("|") if p.strip()]
            # "UK-Kent-Chatham" reads better, and filters better, as "Chatham, UK"
            tidy = []
            for p in parts:
                bits = [b for b in p.split("-") if b]
                tidy.append(f"{', '.join(bits[1:])}, {bits[0]}" if len(bits) > 1 else p)
            loc = " / ".join(tidy)

        yield Job(
            company=src.company,
            title=title,
            url=url,
            platform="icims",
            location=loc,
            remote=_remote(loc, title),
            description="",
            salary=Salary(),
            source_id=src.key,
            flags=["not screened: search listing only, open the advert"],
        )


# --------------------------------------------------------------------------
# Reed
# --------------------------------------------------------------------------
def _screen():
    """screen.py, imported on first use rather than at module import.

    The adapter layer sits below the filter chain, and importing screen.py at
    the top of this file would make `import jobradar.adapters` drag in
    config.py behind it. config.py already defers its own screen.py import for
    the same reason, and this is the other half of that arrangement.
    """
    from .. import screen
    return screen


# Reed employers put the working arrangement in the location field instead of
# a place. screen.py knows "Remote" is not a city and knows to look in the
# body for the country; it does not know these spellings, so "Work From Home"
# came out as the city on the dashboard and as a facet you could filter by.
_REED_NOT_A_PLACE = re.compile(
    r"^\s*(?:work[\s-]?from[\s-]?home|homeworking|home[\s-]?based|home[\s-]?working|"
    r"wfh|remote(?:\s*working)?)\s*$", re.I)


def _reed_location(name: str) -> str:
    """Reed states a town and nothing else, so the country has to be added.

    reed.co.uk is a UK site and `locationName` is free text the employer
    typed: "Stoke-on-Trent", "Cambridgeshire", "City of London". screen.py
    resolves a country from a location string against a city list, and that
    list cannot hold every town and county in Britain: "Stoke-on-Trent" and
    "Cambridgeshire" both resolve to no country at all, and `match` drops a
    posting whose location it cannot place whenever the user has set
    `locations.countries`. Which is every UK user, on the majority of the
    listings, silently.

    So the country is named outright. Only where the location does not already
    name one, because Reed does carry a handful of overseas roles and
    "Dublin, United Kingdom" would file an Irish job as British: the UK marker
    is tested first, and screen.py does not split a location on the comma.

    The test is `names_a_country`, not `_countries_in`. `_countries_in`
    answers on city evidence as well as on country names, so it claimed a bare
    "Perth" for Australia and a bare "Boston" for the United States. Both are
    UK towns with live Reed adverts, both were left without the suffix, and
    both then disappeared for every user filtering on `countries: [UK]`.
    """
    name = (name or "").strip()
    if not name:
        return "United Kingdom"
    if _REED_NOT_A_PLACE.match(name):
        # Keep the country. A Reed listing is a UK listing, and "Remote" on
        # its own is read downstream as "the employer named no country",
        # which sends the role past the country filter untested.
        return "Remote, United Kingdom"
    if _screen().names_a_country(name):
        return name
    return f"{name}, United Kingdom"


def parse_reed(payload: Any, src: Source) -> Iterator[Job]:
    """Reed's jobseeker API: https://www.reed.co.uk/api/1.0/search

    The first aggregator here that is neither an employer's own board nor an
    HTML page, and the reason it earns a place is coverage. Every other source
    is one employer's applicant tracking system, which reaches an employer only
    once somebody has added them; Reed is keyword-driven and reaches the whole
    of its UK market at once, including the mid-size employers who never
    appear on an enumerable board.

    What that costs, and what is done about it:

      * The same role is listed many times, usually once per agency. Reed
        answers that at the query, not here: `postedByDirectEmployer=true`
        asks for employers only, which is what the shipped source uses. Where
        two copies do reach the pipeline, `screen.dedupe` collapses them on
        company plus title and keeps the more direct platform.
      * `employerName` is whoever posted it. On an agency listing that is the
        agency, not the employer, so these roles cannot be trusted to name the
        company they are actually for.
      * The apply link is a reed.co.uk page rather than the employer's own
        form. Only the per-job details endpoint carries `externalUrl`, and
        that is one request per role. Each posting is flagged so the reader
        knows which kind of link they are following.

    Two failure modes worth stating. An empty `results` list means a search
    that matched nothing, which is also what a search for something misspelled
    returns, so liveness here is a result count and never a status code. And a
    missing or wrong API key is a 401, not an empty list, which is the one
    piece of good news: it cannot be mistaken for "no jobs today".
    """
    items = payload.get("results") if isinstance(payload, dict) else payload
    for j in items or []:
        if not isinstance(j, dict):
            continue

        title = _text(j.get("jobTitle"))
        url = _text(j.get("jobUrl"))
        if not url and j.get("jobId") is not None:
            url = f"https://www.reed.co.uk/jobs/{j['jobId']}"
        if not (title and url):
            continue

        desc = _text(j.get("jobDescription") or j.get("description"))
        location = _reed_location(_text(j.get("locationName")))

        sal = from_reed(j)
        if not sal.confirmed:
            # Second go at an unlabelled rate. from_reed will not guess
            # whether a bare 650 is a day rate or an hourly one, but the
            # advert almost always spells it out, and parse_text reads
            # "per day" and "per hour".
            from_advert = parse_text(desc[:1500], default_currency=sal.currency)
            if from_advert.confirmed:
                sal = from_advert

        # Reed has no remote field of any kind, so the working arrangement can
        # only come from the words. Ask screen.py rather than re-deriving it:
        # it checks hybrid BEFORE remote, which is what stops a "hybrid, 2 days
        # in the London office" advert being handed to a remote filter as a
        # remote job on the strength of containing the word.
        probe = Job(company="", title=title, url=url, platform="reed",
                    location=location, description=desc)
        mode = _screen().work_mode(probe)
        if mode == "remote":
            remote: bool | None = True
        elif mode in ("hybrid", "office"):
            remote = False
        else:
            remote = _remote(location, title)

        job = Job(
            company=_text(j.get("employerName")) or "Unknown employer",
            title=title,
            url=url,
            platform="reed",
            location=location,
            remote=remote,
            department=None,
            # Reed writes dates as dd/MM/yyyy, which `_iso` already handles.
            # `date` is the search field and `datePosted` the details one.
            posted_at=_iso(j.get("date") or j.get("datePosted")),
            description=desc,
            salary=sal,
            source_id=src.key,
        )
        job.flags.append("listed on Reed; the apply link goes via reed.co.uk")
        exp = _text(j.get("expirationDate"))
        if exp:
            job.flags.append(f"closes {exp}")
        yield job


# --------------------------------------------------------------------------
# Adzuna
# --------------------------------------------------------------------------
# Adzuna runs one index per country and the country is in the URL path, not in
# the payload: /v1/api/jobs/gb/search/1 is the British index and every figure
# in it is in pounds. Nothing in a result names the country, so an adapter that
# reads only the payload produces "Reading, Berkshire" with no country, and
# `match` drops a posting it cannot place the moment `locations.countries` is
# set. That is the same failure Reed had, arriving by a different route.
_ADZUNA_COUNTRIES = {
    "gb": ("United Kingdom", "GBP"), "us": ("United States", "USD"),
    "at": ("Austria", "EUR"), "au": ("Australia", "AUD"),
    "be": ("Belgium", "EUR"), "br": ("Brazil", "BRL"),
    "ca": ("Canada", "CAD"), "ch": ("Switzerland", "CHF"),
    "de": ("Germany", "EUR"), "es": ("Spain", "EUR"),
    "fr": ("France", "EUR"), "in": ("India", "INR"),
    "it": ("Italy", "EUR"), "mx": ("Mexico", "MXN"),
    "nl": ("Netherlands", "EUR"), "nz": ("New Zealand", "NZD"),
    "pl": ("Poland", "PLN"), "sg": ("Singapore", "SGD"),
    "za": ("South Africa", "ZAR"),
}

_ADZUNA_PATH = re.compile(r"/v1/api/jobs/([a-z]{2})/search/", re.I)


def adzuna_country(url: str) -> tuple[str, str]:
    """The country name and currency behind an Adzuna search URL.

    Falls back to the British index because that is what the shipped builder
    produces, but the code is read from the URL first so pointing the source at
    /jobs/ca/ or /jobs/au/ works without touching this file. An unknown code
    yields no country name at all rather than a wrong one: naming the wrong
    country is worse than naming none, because a wrong name passes the filter.
    """
    m = _ADZUNA_PATH.search(url or "")
    code = (m.group(1) if m else "gb").lower()
    return _ADZUNA_COUNTRIES.get(code, ("", ""))


def _adzuna_location(display: str, country: str) -> str:
    """Adzuna's `display_name` is a town and a county, never a country.

    Same treatment as `_reed_location`, and for the same reason: screen.py
    resolves a country from a city list that cannot hold every town in
    Britain, and an unplaceable location is a dropped posting. The country is
    only added where the string does not already NAME one, so a listing on the
    British index that says "Dublin, Ireland" is not relabelled as British,
    while "Perth" on that index keeps the suffix instead of being handed to
    Australia by the city list.
    """
    display = (display or "").strip()
    if not country:
        return display
    if not display:
        return country
    if _screen().names_a_country(display):
        return display
    return f"{display}, {country}"


def parse_adzuna(payload: Any, src: Source) -> Iterator[Job]:
    """Adzuna's search API: https://api.adzuna.com/v1/api/jobs/{country}/search/{page}

    A keyword-driven aggregator like Reed, and it earns its place for the same
    reason: it reaches employers nobody has added to the source list. It is
    broader than Reed in one way that matters here, which is that it runs
    nineteen national indexes, so the same config that watches the UK can watch
    the United States, Canada and Australia by changing two letters in a URL.

    Four things about the payload that cost a role each if missed:

      * **The salary may be a guess.** `salary_is_predicted` is "1" when the
        number came from Adzuna's Jobsworth model rather than the advertiser.
        `from_adzuna` refuses to confirm those, because only a confirmed figure
        can disqualify a posting and a modelled one would do it silently.
      * **The country is in the URL, not the payload.** See `adzuna_country`.
      * **There is no remote field**, so the arrangement comes from the words,
        via `screen.work_mode`, which tests hybrid before remote.
      * **The description is truncated to 500 characters** by Adzuna's own
        documentation, so it is a preview and not the advert. `enrich` cannot
        expand it either: `redirect_url` is a redirector rather than a page.

    Adzuna has no direct-employer filter of any kind, unlike Reed's
    `postedByDirectEmployer`, so agency listings arrive mixed in with employer
    ones and `company.display_name` is whoever placed the advert.
    """
    items = payload.get("results") if isinstance(payload, dict) else payload
    country, currency = adzuna_country(src.url)

    for j in items or []:
        if not isinstance(j, dict):
            continue

        title = _text(j.get("title"))
        # `redirect_url` is the link Adzuna's terms require you to send people
        # to, and it is also the only one that reaches the advertiser.
        url = _text(j.get("redirect_url"))
        if not (title and url):
            continue

        desc = _text(j.get("description"))
        loc = j.get("location")
        display = _text(loc.get("display_name")) if isinstance(loc, dict) else _text(loc)
        location = _adzuna_location(display, country)

        sal = from_adzuna(j, currency)
        if not sal.confirmed:
            # The truncated advert gets a go at it, exactly as with Reed. An
            # employer who wrote "£150,000 - £170,000" into the first line of
            # the advert beats both silence and a Jobsworth estimate.
            from_advert = parse_text(desc, default_currency=currency)
            if from_advert.confirmed:
                sal = from_advert

        probe = Job(company="", title=title, url=url, platform="adzuna",
                    location=location, description=desc)
        mode = _screen().work_mode(probe)
        if mode == "remote":
            remote: bool | None = True
        elif mode in ("hybrid", "office"):
            remote = False
        else:
            remote = _remote(location, title)

        company = j.get("company")
        category = j.get("category")
        job = Job(
            company=(_text(company.get("display_name")) if isinstance(company, dict)
                     else _text(company)) or "Unknown employer",
            title=title,
            url=url,
            platform="adzuna",
            location=location,
            remote=remote,
            department=(_text(category.get("label"))
                        if isinstance(category, dict) else "") or None,
            posted_at=_iso(j.get("created")),
            description=desc,
            salary=sal,
            source_id=src.key,
        )
        job.flags.append("listed on Adzuna; the apply link redirects to the "
                         "advertiser")
        # A contract advertised at a day rate is annualised by Adzuna before we
        # ever see it, which is how a six month contract clears a permanent
        # salary floor. Say which kind of job it is on the row rather than
        # trying to undo the arithmetic.
        # Both, and they must agree. This field was read into `flags` as
        # display text for months while `job.employment`, which is what the
        # dashboard facets and filters on, was set independently from the
        # prose. A row could carry "contract, not permanent" in its flags and
        # `employment: unstated` at the same time, with nothing anywhere
        # asserting the two should match.
        job.employment = from_platform(j.get("contract_type"))
        if str(j.get("contract_type") or "").lower() == "contract":
            job.flags.append("contract, not permanent")
        if str(j.get("contract_time") or "").lower() == "part_time":
            job.flags.append("part time")
        if not sal.confirmed and str(j.get("salary_is_predicted") or "") == "1":
            job.flags.append("pay figure is an Adzuna estimate, not the "
                             "employer's")
        yield job
