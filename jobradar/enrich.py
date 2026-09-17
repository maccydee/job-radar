"""Fetch the full posting for roles whose source only returned a headline.

LinkedIn's search endpoint returns a title, a company and a location. Nothing
else. That is a quarter to a half of a typical board, and for every one of
those roles the tool was silently doing nothing: dealbreakers had no text to
run against, the salary floor had no figure to compare, `rank` skipped them
because there was nothing to judge fit on, and `generate` refused outright.
They were leads pretending to be matches.

LinkedIn publishes each posting separately, one job id at a time, and that
response carries the whole description. So the missing text is one request per
role rather than something the design has to live without.

This is a read. It spends no tokens. It is also 125 requests to somebody
else's servers on a normal run, so it goes one at a time with a pause, skips
anything it already has, and gives up on a role quietly rather than retrying
it into the ground.

The robots.txt position is the same one the README already discloses for the
search endpoint: LinkedIn disallows it, this reads it anyway, and that is a
deliberate choice a user should know they are making.

ROBOTS.TXT, for the whole module and not only LinkedIn: nothing here fetches
or honours a robots.txt, on the owner's standing instruction. That covers the
posting pages read by every fetcher below (Workday CXS, SmartRecruiters' API,
Breezy, BambooHR, Jobvite, JazzHR, Taleo, iCIMS, Oracle Recruiting Cloud,
Avature and SuccessFactors RMK) and the URL-shape fallback that sends a role
to one of them by the host in its own link. What it does honour is rate:
`fetch.HostLimiter` paces every host separately, one posting at a time.

BOT PROTECTION is not worked around anywhere in here. A 403, a CAPTCHA, a
JavaScript challenge or a page that only renders behind a minted token is
recorded as a failed fetch and the role stays unenriched. The two shape
changes below that look like workarounds are not: `in_iframe=1` is the same
public parameter iCIMS' own search results are served with, and Workday's CXS
endpoint is the JSON the employer's own careers page calls.
"""

from __future__ import annotations

import html as _h
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote

import requests

from . import fetch as fetch_mod, salary as sal_mod, store

# One posting, by id. The same guest surface the search endpoint lives on.
JOB_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# The description sits in one block. Everything else on the page is chrome.
# Capture what is INSIDE the block. Matching from the class name onwards left
# `description__text description__text--rich">` sitting at the front of every
# description, which is noise in the token budget and in anything reading it.
_BLOCK = re.compile(r'description__text[^>]*>(.*?)</section>', re.S)
_TAG = re.compile(r"<[^>]+>")
_BR = re.compile(r"<br\s*/?>|</p>|</li>", re.I)

# Trailing digits of a LinkedIn job URL are its id.
_JOB_ID = re.compile(r"(\d{6,})(?:[/?#]|$)")


def job_id(url: str) -> str:
    m = _JOB_ID.search((url or "").split("?")[0])
    return m.group(1) if m else ""


def _text(page: str) -> str:
    m = _BLOCK.search(page)
    if not m:
        return ""
    # Keep the line breaks: a description that arrives as one wall of text
    # loses the bullet structure the dealbreaker patterns read best against.
    body = _BR.sub("\n", m.group(1))
    body = _h.unescape(_TAG.sub(" ", body))
    lines = [" ".join(x.split()) for x in body.split("\n")]
    return "\n".join(x for x in lines if x).strip()


def fetch(url: str, session=None, timeout: int = 20) -> str:
    jid = job_id(url)
    if not jid:
        return ""
    get = (session or requests).get
    try:
        r = get(JOB_URL.format(job_id=jid), headers={"User-Agent": UA},
                timeout=timeout)
    except requests.RequestException:
        return ""
    if r.status_code != 200:
        return ""
    return _text(r.text)


# Workday and SmartRecruiters both omit the description from their list
# endpoints and both publish it per job. Same shape of problem as LinkedIn,
# same fix, so they share the machinery rather than each growing their own.
_WD_URL = re.compile(r"https://([^/]+)/([a-z]{2}-[A-Z]{2}/)?([^/]+)/job/(.+)$")


def _workday_api(url: str) -> str:
    """Turn a human Workday URL into its CXS one.

    https://x.wd5.myworkdayjobs.com/en-US/site/job/City/Title_R123
      -> https://x.wd5.myworkdayjobs.com/wday/cxs/x/site/job/City/Title_R123

    The tail is trimmed of a trailing slash and of an `/apply` segment before
    it is used. CXS answers **406 Not Acceptable** for either, with a 104 byte
    body and no hint of what it objected to, which is indistinguishable here
    from a dead requisition. `parse_workday` builds clean URLs so the board
    path never hit this, but the URLs that arrive from somewhere else do:
    every Phenom board whose apply link points at the employer's Workday
    tenant carries the `/apply` form, and measured on live tenants (Thales,
    GE HealthCare) the same requisition answers 406 with the suffix and 200
    with 8,035 and 8,873 characters of advert without it.
    """
    m = _WD_URL.match((url or "").split("?")[0])
    if not m:
        return ""
    host, _lang, site, path = m.groups()
    path = re.sub(r"/apply/?$", "", path).rstrip("/")
    if not path:
        return ""
    tenant = host.split(".")[0]
    return f"https://{host}/wday/cxs/{tenant}/{site}/job/{path}"


def _from_workday(url: str, session=None, timeout: int = 20) -> str:
    api = _workday_api(url)
    if not api:
        return ""
    get = (session or requests).get
    try:
        r = get(api, headers={"User-Agent": UA, "Accept": "application/json"},
                timeout=timeout)
        if r.status_code != 200:
            return ""
        info = (r.json() or {}).get("jobPostingInfo") or {}
    except (requests.RequestException, ValueError):
        return ""
    return _strip(info.get("jobDescription") or "")


# `/posting/` and `/postings/` are the OLD public path and it 404s. It is also
# the only shape this pattern used to accept, and `parse_smartrecruiters` was
# fixed to stop producing it: the live link is
# `https://jobs.smartrecruiters.com/<Company>/<20-digit id>` with no segment
# between them. So the board parser was corrected and the enricher was not, and
# the two have disagreed ever since. The measured effect is total: 269 of 269
# SmartRecruiters roles in a 244-board scan arrived with no description (the
# list endpoint has no `jobAd`), and 0 of 25 sampled were enriched, because
# every URL failed this match before a request was ever made. 910 bundled
# boards, 5.1% of the list.
#
# Both shapes are accepted rather than swapped, because a role stored before
# the parser was fixed still carries the old URL in the database.
_SR_URL = re.compile(
    r"smartrecruiters\.com/([^/?#]+)/(?:postings?/)?(\d+)")


def _from_smartrecruiters(url: str, session=None, timeout: int = 20) -> str:
    m = _SR_URL.search(url or "")
    if not m:
        return ""
    company, posting = m.groups()
    get = (session or requests).get
    try:
        r = get(f"https://api.smartrecruiters.com/v1/companies/{company}"
                f"/postings/{posting}", timeout=timeout)
        if r.status_code != 200:
            return ""
        secs = ((r.json() or {}).get("jobAd") or {}).get("sections") or {}
    except (requests.RequestException, ValueError):
        return ""
    # Keep the qualifications section: it is where the must-haves live, which
    # is what dealbreakers and fit are actually judged on.
    order = ("jobDescription", "qualifications", "additionalInformation",
             "companyDescription")
    return _strip("\n\n".join((secs.get(k) or {}).get("text") or ""
                               for k in order if secs.get(k)))


def _strip(markup: str) -> str:
    body = _BR.sub("\n", markup or "")
    body = _h.unescape(_TAG.sub(" ", body))
    lines = [" ".join(x.split()) for x in body.split("\n")]
    return "\n".join(x for x in lines if x).strip()


# Breezy publishes schema.org JSON-LD on every posting page so that Google
# Jobs can index it. That block carries the whole advert, which the `/json`
# board endpoint does not carry at all, so this is reading a documented
# structure rather than scraping their markup.
_LD_BLOCK = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I)


def _json_ld_text(page: str) -> str:
    """The advert out of a page's schema.org JobPosting block.

    Split out from `_from_json_ld` because iCIMS needs the same reader against
    a URL it will not serve unmodified: see `_from_icims`.
    """
    for m in _LD_BLOCK.finditer(page or ""):
        try:
            node = json.loads(m.group(1))
        except ValueError:
            continue
        # There are two blocks on a Breezy page and the first one is a WebSite,
        # so taking the first match returned an empty description every time.
        for d in (node if isinstance(node, list) else [node]):
            if isinstance(d, dict) and d.get("@type") == "JobPosting":
                return _strip(d.get("description") or "")
    return ""


def _page(url: str, session=None, timeout: int = 20) -> str:
    """A posting page's HTML, or "" for anything that is not a clean 200.

    The board links carry `?source=...` on some postings; the page is the same
    without it and the shorter URL is what the seen-set is keyed on.
    """
    get = (session or requests).get
    try:
        r = get((url or "").split("?")[0], headers={"User-Agent": UA},
                timeout=timeout)
    except requests.RequestException:
        return ""
    return r.text if r.status_code == 200 else ""


def _from_json_ld(url: str, session=None, timeout: int = 20) -> str:
    """The advert out of a posting page's schema.org JobPosting block.

    Breezy, Jobvite and Avature all publish one so that Google Jobs can index
    them, and none of them puts the advert in its list endpoint. Same problem,
    same fix, so they share this rather than each growing their own copy.
    """
    return _json_ld_text(_page(url, session, timeout))


_from_breezy = _from_json_ld

# Jobvite publishes a JobPosting block on most tenants and none at all on
# some: `ness`, `traffictech` and `edgeautonomy-careers` all carry one, while
# `savers` and `monarchinvestment` carry zero blocks of any type on a
# perfectly healthy 200, so the shared reader returns "" and the role stays
# unscreenable. Measured on a 244-board scan: 3 of 13 sampled Jobvite postings
# that answered 200 had no JSON-LD.
#
# The advert on those pages sits in `<div class="jv-job-detail-description">`,
# which is Jobvite's own template markup rather than an employer theme, and it
# is the same class on the tenants that do publish JSON-LD. `ng-non-bindable`
# sits on the same tag on some pages and not others, so the class is matched
# as a word anywhere in the tag.
#
# Counted rather than lazy for the same reason `_from_rmk` counts: the advert
# is a chain of nested divs (the Savers posting nests four deep immediately),
# and a lazy `(.*?)</div>` stops at the first inner close and returns a
# fragment, which over 200 characters would be stored and treated as the whole
# advert.
_JV_DESC_OPEN = re.compile(
    r'<div[^>]*\bclass="[^"]*\bjv-job-detail-description\b[^"]*"[^>]*>', re.I)


def _from_jobvite(url: str, session=None, timeout: int = 20) -> str:
    page = _page(url, session, timeout)
    if not page:
        return ""
    text = _json_ld_text(page)
    if text:
        return text
    for body in _inner_blocks(page, _JV_DESC_OPEN, "div"):
        return _strip_blocks(body)
    return ""


# JazzHR publishes an Organization block on the posting page but no
# JobPosting one, so the shared JSON-LD reader comes back empty. The advert
# itself sits in a div with a stable id, which is what this reads instead.
_JZ_DESC = re.compile(
    r'<div[^>]+id="job-description"[^>]*>(.*?)</div>\s*(?:<div|<section|<footer)',
    re.S | re.I)


def _from_jazzhr(url: str, session=None, timeout: int = 20) -> str:
    get = (session or requests).get
    try:
        r = get((url or "").split("?")[0], headers={"User-Agent": UA},
                timeout=timeout)
    except requests.RequestException:
        return ""
    if r.status_code != 200:
        return ""
    m = _JZ_DESC.search(r.text)
    return _strip(m.group(1)) if m else ""


# Taleo's posting page renders itself from JavaScript too, so there is no
# JobPosting JSON-LD and no description element to read. The advert is in a
# `api.fillList(... 'descRequisition', [...])` array, URL-encoded, and the
# INDEX of it moves: it is element 10 on TTEC and element 11 on BAE Systems,
# because BAE's career section adds a job-field pair that TTEC's does not.
# Every other element in the array is a requisition id, a boolean, a location
# or a one-line label, so the advert is simply the longest one. Reading it by
# position instead would return the string "false" for half the platform.
_TL_DESC_LIST = re.compile(
    r"api\.fillList\(\s*'requisitionDescriptionInterface'\s*,\s*"
    r"'descRequisition'\s*,\s*\[(.*?)\]\s*\)\s*;", re.S)
_TL_DESC_ITEM = re.compile(r"'((?:[^'\\]|\\.)*)'", re.S)


def _from_taleo(url: str, session=None, timeout: int = 20) -> str:
    get = (session or requests).get
    try:
        r = get(url or "", headers={"User-Agent": UA}, timeout=timeout)
    except requests.RequestException:
        return ""
    if r.status_code != 200:
        return ""
    m = _TL_DESC_LIST.search(r.text)
    if not m:
        return ""
    best = ""
    for item in _TL_DESC_ITEM.findall(m.group(1)):
        # `!*!` is Taleo's own marker for "this element is rich text", not part
        # of the advert, and it lands at the front of the description.
        text = _strip(unquote(item.replace("!*!", "")))
        if len(text) > len(best):
            best = text
    return best


# BambooHR's `/careers/list` is a summary index: no advert text, no salary, no
# date. The advert lives one request away at `/careers/<id>/detail`, which is
# the same JSON API the board itself is built on rather than a page scrape.
def _from_bamboohr(url: str, session=None, timeout: int = 20) -> str:
    base = (url or "").split("?")[0].rstrip("/")
    if not re.search(r"bamboohr\.com/careers/\d+$", base):
        return ""
    get = (session or requests).get
    try:
        r = get(f"{base}/detail", headers={"User-Agent": UA,
                                           "Accept": "application/json"},
                timeout=timeout)
        if r.status_code != 200:
            return ""
        job = ((r.json() or {}).get("result") or {}).get("jobOpening") or {}
    except (requests.RequestException, ValueError):
        return ""
    text = _strip(job.get("description") or "")
    # The detail record has a `compensation` string the list endpoint does not.
    # Putting it in front of the advert is what lets `run()` re-parse pay for
    # these roles, which otherwise carry no figure from anywhere.
    pay = (job.get("compensation") or "").strip()
    return f"Compensation: {pay}\n\n{text}".strip() if pay else text


# Oracle and SuccessFactors both write their adverts as a chain of <div>s with
# no <p> or <li> in them at all, so `_strip`'s line breaks never fire and the
# whole advert arrives as one unbroken line. The dealbreaker patterns read
# best against bullet structure, and a heading welded to the sentence after it
# ("We are Reckitt Home to the world's best loved brands") also reads as one
# phrase to anything scanning for a job title or a seniority word.
_BLOCK_END = re.compile(r"</(?:div|h[1-6]|tr|table|ul|ol)>", re.I)


def _strip_blocks(markup: str) -> str:
    return _strip(_BLOCK_END.sub("\n", markup or ""))


def _inner_blocks(page: str, opener, tag: str):
    """The inside of every element `opener` matches, closing tags counted.

    A lazy `(.*?)</span>` is what this replaces and it is not a small
    difference: both Avature's and SuccessFactors' advert containers nest
    further elements of the same tag inside themselves (EA's Avature advert
    holds 12 more divs, PSEG's SuccessFactors advert enough spans that a lazy
    match returns 121 characters of its 15,758), so the lazy form stops at the
    first inner close and yields a fragment. A fragment over 200 characters
    gets stored and treated as the whole advert, which is worse than returning
    nothing, because nothing at least leaves the role visibly unscreened.
    """
    pair = re.compile(rf"<{tag}\b|</{tag}>", re.I)
    for m in opener.finditer(page or ""):
        rest = page[m.end():]
        depth = 1
        for t in pair.finditer(rest):
            depth += -1 if t.group(0).lower() == f"</{tag}>" else 1
            if depth == 0:
                yield rest[:t.start()]
                break
        else:
            # Unbalanced markup: take what is there rather than dropping the
            # advert entirely.
            yield rest


# iCIMS renders its posting into an iframe exactly the way it renders its
# search results into one. The bare posting URL answers 200 with a ~3.8KB
# shell that contains no advert and no JSON-LD, so the shared reader returns
# "" on a page that looks perfectly healthy. `in_iframe=1` returns the
# server-rendered posting, JobPosting block and all. This is the same trick
# `parse_icims` already uses on the search endpoint.
#
# The query string is rebuilt rather than kept, because a stored URL that lost
# its `in_iframe=1` somewhere (a redirect, a hand-edited source) would enrich
# to nothing with no error to show for it.
_ICIMS_JOB = re.compile(r"/jobs/\d+/", re.I)
_ICIMS_WALL = re.compile(r"/(?:login|register)/?$", re.I)


def _from_icims(url: str, session=None, timeout: int = 20) -> str:
    base = (url or "").split("?")[0]
    if not _ICIMS_JOB.search(base):
        return ""
    # `.../job/login` is the sign-in wall in front of the same requisition,
    # and it answers 200 with a 28KB page carrying no JobPosting block at all,
    # so it reads as a healthy page with no advert on it. Dropping the suffix
    # returns the posting: measured on two Orange requisitions, 0 characters
    # at `/job/login` against 4,746 and 5,990 at `/job`. These URLs arrive from
    # Phenom boards whose apply link points at the employer's iCIMS tenant.
    base = _ICIMS_WALL.sub("", base)
    get = (session or requests).get
    try:
        r = get(f"{base}?in_iframe=1", headers={"User-Agent": UA},
                timeout=timeout)
    except requests.RequestException:
        return ""
    if r.status_code != 200:
        return ""
    return _json_ld_text(r.text)


# Oracle Recruiting Cloud's posting page is a JavaScript shell: 4.4KB, no
# JSON-LD, no advert. The text is in the same REST API `parse_oracle` already
# reads the list from, one requisition at a time.
#
# The resource is `recruitingCEJobRequisitionDetails`, PLURAL. The singular
# spelling answers 404 with an empty body, which is indistinguishable from a
# dead board, so this is not a name to guess at.
#
# The site number has to come from the URL. It is CX_1 on most tenants but not
# all, and `parse_oracle` already carries whatever the source said through
# into the job URL, so it is read back out here rather than assumed.
_ORACLE_JOB = re.compile(
    r"^https?://([^/]+)/hcmUI/CandidateExperience/[^/]+/sites/([^/]+)/job/([^/?#]+)",
    re.I)
_ORACLE_API = ("https://{host}/hcmRestApi/resources/latest/"
               "recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
               "&finder=ById;Id=%22{rid}%22,siteNumber={site}")


def _from_oracle(url: str, session=None, timeout: int = 20) -> str:
    m = _ORACLE_JOB.match(url or "")
    if not m:
        return ""
    host, site, rid = m.groups()
    get = (session or requests).get
    try:
        r = get(_ORACLE_API.format(host=host, rid=rid, site=site),
                headers={"User-Agent": UA, "Accept": "application/json"},
                timeout=timeout)
        if r.status_code != 200:
            return ""
        items = (r.json() or {}).get("items") or []
    except (requests.RequestException, ValueError):
        return ""
    if not items or not isinstance(items[0], dict):
        return ""
    job = items[0]
    # The advert is split across fields and which ones are filled varies by
    # tenant: Marks and Spencer put all 8,762 characters in
    # ExternalDescriptionStr and leave responsibilities and qualifications
    # empty, while Mashreq's ExternalDescriptionStr is a 349 character stub
    # with the real advert in the other two. Reading one field alone loses
    # whole adverts either way.
    #
    # ShortDescriptionStr is deliberately excluded: it is a teaser cut from
    # the description, so including it scores the same paragraph twice.
    order = ("ExternalDescriptionStr", "ExternalResponsibilitiesStr",
             "ExternalQualificationsStr", "CorporateDescriptionStr",
             "OrganizationDescriptionStr")
    parts = []
    for k in order:
        text = _strip_blocks(job.get(k) or "")
        # Some tenants fill every field with the same advert. One measured
        # board had responsibilities and qualifications byte-identical to each
        # other and both a re-encoding of the description, so a plain join
        # stored the advert three times: three times the tokens for `rank` to
        # read and three times the chance of tripping the 20,000 character cap
        # before the end of the advert is reached.
        if not text or any(text in p for p in parts):
            continue
        parts = [p for p in parts if p not in text]
        parts.append(text)
    return "\n\n".join(parts)


# Avature serves a JobPosting block on some tenants and none at all on others:
# Tesco's careers site has one, EA's sandbox board has zero, and both are
# ordinary Avature installs. So the shared reader is tried first and a second
# reader stands behind it rather than the platform being called done on the
# strength of one board that happened to work.
#
# The fallback reads Avature's own template class rather than an employer
# theme's markup, which is why it holds across tenants: the same
# `article__content__view__field__value` divs are on the Tesco page that did
# not need them. Every field block is taken, not just the advert one, because
# the header of the advert block is localised ("Descripción del puesto") and
# keying on it would fail on exactly the non-English boards this is for. The
# other blocks are the location, the worker type and the req id, which are
# short and are worth screening against anyway.
_AV_FIELD = re.compile(
    r'<div[^>]*\bclass="[^"]*\barticle__content__view__field__value\b[^"]*"[^>]*>',
    re.I)

# Avature ships a second, older page template that has neither of the two
# things above: jobs.colorado.edu is an ordinary Avature tenant serving
# `/jobs/JobDetail/<slug>/<id>` with zero JSON-LD blocks and zero
# `article__content__view__field__value` divs, so both readers returned "" on
# a healthy 200 and the role stayed unscreenable.
#
# What that template does carry is schema.org as MICRODATA rather than
# JSON-LD: `<div class="jobDetail" itemscope itemtype=".../JobPosting">` with
# eleven `itemprop="description"` divs inside it, one per accordion section
# (Job Summary, Duties, Qualifications...). Reading every one of them and
# joining is what keeps the qualifications, which is where the must-haves are
# and what the dealbreakers are actually judged on.
#
# `itemprop` is matched rather than the theme's `jobDetailDescription` class,
# because the class is part of the employer's skin and the microdata is part
# of the schema.org contract the page is claiming to honour.
_AV_MICRO = re.compile(r'<div[^>]*\bitemprop="description"[^>]*>', re.I)


def _from_avature(url: str, session=None, timeout: int = 20) -> str:
    page = _page(url, session, timeout)
    if not page:
        return ""
    text = _json_ld_text(page)
    if text:
        return text
    parts = [_strip_blocks(b) for b in _inner_blocks(page, _AV_FIELD, "div")]
    out = "\n\n".join(p for p in parts if p).strip()
    if len(out) >= 200:
        return out
    # The field blocks are the location, the worker type and the req id when
    # there is no advert block among them, which is about 86 characters and
    # is not a description. Below the floor `run()` would discard it anyway,
    # so trying the microdata costs nothing and recovers the whole advert.
    micro = [_strip_blocks(b) for b in _inner_blocks(page, _AV_MICRO, "div")]
    joined = "\n\n".join(p for p in micro if p).strip()
    return joined if len(joined) > len(out) else out


# SuccessFactors RMK publishes no JSON-LD at all: zero blocks on Reckitt and
# zero on Burberry, so the shared reader comes back empty on a 200. The advert
# sits in <span class="jobdescription">, which is stable across tenants
# because it is SuccessFactors' own markup rather than the employer's theme.
#
# The close is found by counting, not by a lazy `(.*?)</span>`, because most
# adverts nest further spans inside that one. Measured on live boards: PSEG's
# advert is 15,758 characters and a lazy match returns 121 of them, Cintas
# 5,124 against 133, Medibank 8,354 against 153. Every one of those would be
# under the 200-character floor and so would look like a failed fetch, but
# Hikma's returns 1,053 of 2,375, which would be stored as the whole advert.
#
# The opening tag varies: some tenants serve `<span class="jobdescription">`
# and others `<span itemprop="description" class="jobdescription">`, so the
# class is matched as a word anywhere in the tag rather than as the whole
# attribute.
_RMK_DESC_OPEN = re.compile(
    r'<span[^>]*\bclass="[^"]*\bjobdescription\b[^"]*"[^>]*>', re.I)


def _from_rmk(url: str, session=None, timeout: int = 20) -> str:
    page = _page(url, session, timeout)
    if not page:
        return ""
    for body in _inner_blocks(page, _RMK_DESC_OPEN, "span"):
        return _strip_blocks(body)
    return ""


# Which fetcher handles which platform. A platform absent from here is one
# whose list endpoint already carries the description.
# Phenom PCSX. The search endpoint carries no advert at all, so every role off
# a PCSX board arrives unscreened and this is the only thing that fixes that.
# The tenant is a `domain` query parameter rather than part of the path, and
# it is the registrable domain, not the host: apply.careers.microsoft.com asks
# as microsoft.com.
_PCSX_URL = re.compile(r"//([^/]*)/careers/job/(\d+)", re.I)


def _from_pcsx(url: str, session=None, timeout: int = 20) -> str:
    m = _PCSX_URL.search(url or "")
    if not m:
        return ""
    host, position = m.groups()
    domain = ".".join(host.split(".")[-2:])
    get = (session or requests).get
    try:
        r = get(f"https://{host}/api/pcsx/position_details"
                f"?position_id={position}&domain={domain}&hl=en",
                timeout=timeout)
        if r.status_code != 200:
            return ""
        body = r.json() or {}
    except (requests.RequestException, ValueError):
        return ""
    data = body.get("data") or body
    return _strip(data.get("jobDescription") or "")


class Details(str):
    """An advert text that also carries structured fields from its source.

    A `str` subclass so that every caller of a fetcher, `_try` included, keeps
    treating it as the text it always was, and only `run()` looks further.
    Reed is the first fetcher whose per-job response states pay and contract
    type as fields rather than prose, and throwing those away to fit the
    string-only contract would leave the day rate exactly as unlabelled as
    the search left it.

    `error` is set on a FAILED fetch and the text is then "". It exists so a
    refusal can be told apart from an advert that is simply not there: a 401
    means the key is wrong for every Reed role in the run, not that this one
    posting has no description.
    """

    salary = None
    employment = ""
    error = ""
    refused = False

    @classmethod
    def make(cls, text: str = "", *, salary=None, employment: str = "",
             error: str = "", refused: bool = False) -> "Details":
        d = cls(text or "")
        d.salary, d.employment = salary, employment
        d.error, d.refused = error, refused
        return d


# Reed's per-job endpoint. The search endpoint returns a 453 character extract
# and a bare figure with no period (checked on 17 Sept 2026: its result rows
# carry jobId, employerId, employerName, employerProfileId,
# employerProfileName, jobTitle, locationName, minimumSalary, maximumSalary,
# currency, date, expirationDate, jobDescription, applications and jobUrl, and
# nothing else). This one carries the whole advert as HTML, `salaryType`,
# `contractType` and `externalUrl`.
REED_JOB_API = "https://www.reed.co.uk/api/1.0/jobs/{job_id}"
_REED_ID = re.compile(r"reed\.co\.uk/jobs/(?:[^/?#]+/)?(\d+)(?:[/?#]|$)", re.I)


def reed_details(payload) -> Details:
    """The parts of a Reed details response that `run()` stores.

    Split from the request so that a captured payload can be tested without a
    key or a network.

    `externalUrl`, the employer's own apply link, is not read. The roles table
    has no column for an apply link separate from the posting URL, and
    overwriting `url` with it would change the uid-bearing address of a role
    already on the board.
    """
    from . import employment as emp_mod
    if not isinstance(payload, dict):
        return Details.make(error="details response was not an object")
    text = _strip(payload.get("jobDescription") or "")
    # Only a salaryType Reed actually stated can confirm a figure: see
    # `salary.from_reed`, which leaves an unknown one unconfirmed.
    sal = sal_mod.from_reed(payload)
    # "Temporary" is what Reed sends for both a day-rate contract and a
    # 12-month fixed term (Techtronic's "Security & Network Engineer - 12
    # months Fixed term" arrived as Temporary), and "Permanent" for a
    # permanent job. `from_platform` already maps both, and anything it does
    # not know stays unstated rather than being read as one of the two.
    emp = emp_mod.from_platform(payload.get("contractType"))
    return Details.make(text, salary=sal, employment=emp)


def _from_reed(url: str, session=None, timeout: int = 20,
               api_key: str = "") -> Details:
    m = _REED_ID.search(url or "")
    if not m:
        return Details.make(error="no Reed job id in the URL")
    if not api_key:
        # Not asked at all. Reed answers an unkeyed request with 401, and
        # sending one per role to learn what is already known is impolite.
        return Details.make(error="no Reed API key", refused=True)
    get = (session or requests).get
    try:
        # Auth on the REQUEST, never on the session. The session is the worker
        # thread's and is reused for every other host that thread fetches, and
        # `fetch_reed` records what setting `session.auth` once did: the key
        # went out in the Authorization header to thousands of third parties.
        r = get(REED_JOB_API.format(job_id=m.group(1)), auth=(api_key, ""),
                headers={"Accept": "application/json"}, timeout=timeout)
    except requests.RequestException as e:
        return Details.make(error=f"request failed: {type(e).__name__}")
    if r.status_code in (401, 403):
        return Details.make(error=f"Reed refused the API key (HTTP {r.status_code})",
                            refused=True)
    if r.status_code != 200:
        return Details.make(error=f"HTTP {r.status_code}")
    try:
        return reed_details(r.json())
    except ValueError:
        return Details.make(error="details response was not JSON")


FETCHERS = {
    "linkedin": lambda u, s: fetch(u, session=s),
    "reed": _from_reed,
    "workday": _from_workday,
    "smartrecruiters": _from_smartrecruiters,
    "breezy": _from_breezy,
    "bamboohr": _from_bamboohr,
    "jobvite": _from_jobvite,
    "jazzhr": _from_jazzhr,
    "taleo": _from_taleo,
    "icims": _from_icims,
    "oracle": _from_oracle,
    "avature": _from_avature,
    "rmk": _from_rmk,
    "pcsx": _from_pcsx,
}


# The platform column says which BOARD a role came off. It does not say which
# system publishes the advert, and for two of the bundled platforms those are
# routinely different systems:
#
#   * Phenom's `applyUrl` is the employer's real ATS. Measured over 1,882
#     Phenom roles from 18 boards: 1,562 point at a Workday tenant, 73 at
#     iCIMS, 33 at a SuccessFactors jobs2web host. 1,668 of 1,882, 88.6%, are
#     on a system this module already knows how to read.
#   * The two `custom` boards in the bundled list are Atlassian, whose
#     listings endpoint hands back iCIMS posting URLs. All 111 roles, all
#     iCIMS, all previously enriched by nothing at all because "custom" is not
#     a key in FETCHERS.
#
# So dispatch falls back to the shape of the role's own URL. This is not a
# guess about an unknown platform: each pattern is a host that one of the
# fetchers above is already written against, and a URL that matches none of
# them is left alone exactly as before.
#
# The SQL LIKE beside each pattern is what puts these roles in front of
# `candidates()` in the first place. It is kept in this table rather than
# hand-written into the query for the same reason the platform list was moved
# here: two copies of the same fact drift, and the symptom is a fetcher that
# never runs with no error to show for it.
URL_FETCHERS = (
    (re.compile(r"//[^/]*\.myworkdayjobs\.com/", re.I),
     "%.myworkdayjobs.com/%", _from_workday),
    (re.compile(r"//[^/]*myworkdaysite\.com/", re.I),
     "%myworkdaysite.com/%", _from_workday),
    (re.compile(r"//jobs\.smartrecruiters\.com/[^/?#]+/\d+", re.I),
     "%jobs.smartrecruiters.com/%", _from_smartrecruiters),
    (re.compile(r"//[^/]*\.icims\.com/jobs/\d+/", re.I),
     "%.icims.com/jobs/%", _from_icims),
    (re.compile(r"//[^/]*jobs2web\.com/", re.I),
     "%jobs2web.com/%", _from_rmk),
    (re.compile(r"//[^/]*\.avature\.net/", re.I),
     "%.avature.net/%", _from_avature),
    (re.compile(r"//[^/]*\.taleo\.net/", re.I),
     "%.taleo.net/%", _from_taleo),
    (re.compile(r"//[^/]*\.breezy\.hr/p/", re.I),
     "%.breezy.hr/p/%", _from_breezy),
    (re.compile(r"//[^/]*\.bamboohr\.com/careers/\d+", re.I),
     "%.bamboohr.com/careers/%", _from_bamboohr),
    (re.compile(r"//[^/]*\.applytojob\.com/apply/", re.I),
     "%.applytojob.com/apply/%", _from_jazzhr),
    (re.compile(r"//jobs\.jobvite\.com/[^/?#]+/job/", re.I),
     "%jobs.jobvite.com/%", _from_jobvite),
    (re.compile(r"/hcmUI/CandidateExperience/", re.I),
     "%/hcmUI/CandidateExperience/%", _from_oracle),
    (re.compile(r"//[^/]*/careers/job/\d+", re.I),
     "%/careers/job/%", _from_pcsx),
)


# Some list endpoints publish a teaser, not the advert, and a teaser is long
# enough to clear a 200 character floor. That is the worst of the three
# states: the role looks described, `candidates()` never picks it up, and the
# dealbreakers and the salary floor run against a paragraph of marketing.
#
# Measured, from the same 244-board scan:
#   * Phenom's `descriptionTeaser` averages 290 characters. 1,714 of 1,882
#     Phenom roles cleared the old floor on nothing but that teaser.
#   * Oracle's `ShortDescriptionStr` gave 185 of 483 roles a stored
#     description of 200-1,000 characters. Fetching the full requisition for
#     20 of them returned a median of 6,400 characters: every single one was
#     between 3.8x and 16.2x longer than what was stored.
#
# 1,200 is set above every teaser measured and below every real advert
# measured. A genuinely short advert on one of these two platforms costs one
# wasted request per scan, which is the cheap side of the mistake.
STUB_FLOORS = {"phenom": 1200, "oracle": 1200}

# Below this a fetched text is treated as a failed parse rather than an
# advert, because a fragment stored as the whole advert is worse than nothing:
# nothing at least leaves the role visibly unscreened.
MIN_DESC = 200


def fetcher_for(url: str, platform: str = ""):
    """The fetchers to try for one role, best first, at most two.

    The platform's own fetcher goes first because it is the one written
    against that board's stored URL shape. The URL fetcher stands behind it
    for a role whose platform has none, and for one whose platform fetcher
    came back empty on a URL that plainly belongs to another system.
    """
    out = []
    first = FETCHERS.get(platform)
    if first:
        out.append(first)
    for pat, _like, fn in URL_FETCHERS:
        if pat.search(url or "") and fn not in out:
            out.append(fn)
            break
    return out


def _floor_sql() -> str:
    """`LENGTH(...) < <floor>`, with the per-platform stub floors folded in.

    Reed is not a stub floor. Its extract is a fixed 453 characters however
    long the advert, so a floor would either miss it or refetch every short
    advert Reed returned whole on every scan. The test is the same shape
    `screen.is_reed_snippet` applies when it flags the role, kept beside it
    in SQL so that "flagged as an extract" and "queued for its details" can
    only disagree if someone edits one of them.
    """
    from .screen import REED_SNIPPET_MAX
    cases = " ".join(f"WHEN '{p}' THEN {n}" for p, n in STUB_FLOORS.items())
    desc = "TRIM(COALESCE(r.description,''))"
    return (f"(LENGTH({desc}) < "
            f"CASE r.platform {cases} ELSE {MIN_DESC} END "
            f"OR (r.platform = 'reed' AND LENGTH({desc}) <= {REED_SNIPPET_MAX} "
            f"AND ({desc} LIKE '%...' OR {desc} LIKE '%\u2026')))")


def candidates(con, limit: int = 0) -> list:
    """Roles on the board that have a URL we can expand and no description."""
    store._ensure_columns(con)
    likes = [like for _pat, like, _fn in URL_FETCHERS]
    q = ("SELECT r.uid, r.url, r.platform, r.salary_confirmed, r.country, "
         # Title and employment so that a platform-stated contract type can
         # be applied with the same precedence `screen.enrich` uses.
         "r.title, r.employment, "
         # The stored length comes back with the row so that `run()` can
         # refuse to replace a long advert with a short one. Without it the
         # stub floors would happily overwrite Oracle's 653 character teaser
         # with a 300 character parse failure and call that an improvement.
         "LENGTH(TRIM(COALESCE(r.description,''))) AS desc_len FROM roles r "
         "LEFT JOIN role_state s ON s.uid = r.uid "
         "WHERE COALESCE(s.status,'new') NOT IN "
         "('rejected','withdrawn','skipped','closed') "
         f"AND {store.LIVE_SQL} "
         # Was a second, hand-maintained copy of FETCHERS' keys. Adding Breezy
         # to one and not the other writes a fetcher that never runs, and the
         # symptom is silence rather than an error.
         f"AND (r.platform IN ({','.join('?' for _ in FETCHERS)}) "
         f"OR {' OR '.join('r.url LIKE ?' for _ in likes)}) "
         f"AND {_floor_sql()}")
    rows = con.execute(q, (*FETCHERS, *likes)).fetchall()
    return rows[:limit] if limit else rows


def run(con, cfg=None, rows=None, pause: float = 1.0, on_each=None,
        concurrency: int = fetch_mod.DEFAULT_CONCURRENCY,
        notes: list | None = None) -> tuple[int, int]:
    """Fill in descriptions. Returns (fetched, attempted).

    Re-parses pay while it is there: a posting that states a salary in its body
    was being carried as "unconfirmed" purely because the body had never been
    read, which meant the floor could not act on it either.

    `notes`, when given, collects sentences about failures a caller should
    print. A fetch that fails writes nothing and so leaves no trace in the
    database, which for most fetchers is fine: the role stays short and stays
    flagged. A refused API key is different in kind, because it fails every
    Reed role at once for a reason the reader can fix, and a count of
    "filled in 0 of 21" does not say that.
    """
    rows = candidates(con) if rows is None else rows
    keys = {"reed": getattr(cfg, "reed_api_key", "") or ""} if cfg else {}
    got = 0
    failed: dict[str, int] = {}
    for i, r, text in _texts(rows, pause, concurrency, keys):
        err = getattr(text, "error", "")
        if err and (r["platform"] or "") == "reed":
            failed[err] = failed.get(err, 0) + 1
        if text and len(text) >= MIN_DESC and len(text) > _stored_len(r):
            got += 1
            fields = {"description": text[:20000]}
            stated = getattr(text, "salary", None)
            if stated is not None and stated.confirmed:
                # A pay field the platform stated for THIS posting replaces
                # whatever is stored, confirmed or not. What is stored for a
                # Reed role came off the search endpoint, which has no period,
                # so it is at best a guess that a big number is annual and at
                # worst a day rate filed as "year". The salaryType beside the
                # figure is the employer's own answer.
                s = stated
            else:
                # The job's country, never the reader's floor currency. See
                # `salary.CURRENCY_OF_COUNTRY` for what that was doing to a
                # posting priced in rupees.
                s = sal_mod.parse_text(
                    text, sal_mod.currency_of_country(
                        r["country"] if "country" in r.keys() else None))
                if r["salary_confirmed"]:
                    s = None
            if s is not None and s.confirmed:
                fields.update({
                    "salary_min": s.min, "salary_max": s.max,
                    "salary_currency": s.currency, "salary_period": s.period,
                    "salary_confirmed": 1, "salary_label": s.label(),
                })
            emp = _employment(r, getattr(text, "employment", ""), text)
            if emp:
                fields["employment"] = emp
            con.execute(
                "UPDATE roles SET " + ",".join(f"{k}=?" for k in fields)
                + " WHERE uid=?", (*fields.values(), r["uid"]))
        if on_each:
            on_each(i, len(rows), got)
    if notes is not None:
        for err, n in sorted(failed.items(), key=lambda kv: -kv[1]):
            notes.append(f"{n} Reed role(s) kept their search extract: {err}")
    return got, len(rows)


def _employment(row, stated: str, text: str) -> str:
    """The employment value to write after enrichment, or "" to leave it.

    Same precedence as `screen.enrich`, which only ever sees the extract for a
    Reed role: a title that says contract outright wins, then the platform's
    own field, then the full advert's text. "" rather than `unstated` when
    nothing decided it, because a writer that could not classify a posting is
    missing the answer, not contradicting the one already stored.

    The text rule only fills a gap. A stored `permanent` or `contract` may
    have come from a platform's own field on the scan (Workable, Ashby,
    SmartRecruiters and the rest), and nothing in the row says which, so a
    regex over the prose must not replace it.
    """
    from . import employment as emp_mod
    keys = row.keys() if hasattr(row, "keys") else ()
    title = row["title"] if "title" in keys else ""
    if not title:
        # A caller's own query without the title cannot apply the title rule,
        # and applying the other two without it could overwrite a contract
        # title with a platform "Permanent".
        return ""
    by_title, _ = emp_mod.classify(title)
    if by_title == emp_mod.CONTRACT:
        value = emp_mod.CONTRACT
    elif stated and stated != emp_mod.UNSTATED:
        value = stated
    else:
        stored = row["employment"] if "employment" in keys else ""
        if (stored or emp_mod.UNSTATED) != emp_mod.UNSTATED:
            return ""
        value, _ = emp_mod.classify(title, text)
    return "" if value == emp_mod.UNSTATED else value


def _stored_len(row) -> int:
    """How long the description already on the role is, or 0.

    Tolerant of a row that has no `desc_len`, because `run()` takes whatever
    rows it is handed and a caller with its own query should not have to know
    about a column added for the stub floors.
    """
    try:
        return int(row["desc_len"] or 0)
    except (IndexError, KeyError, TypeError, ValueError):
        return 0


def _texts(rows, pause: float, concurrency: int, keys: dict | None = None):
    """Yield (position, row, advert text) with the fetching done in parallel.

    This pass used to be strictly serial with a fixed one second sleep between
    every row, which is the same mistake the scan itself made: a single global
    delay standing in for politeness towards each individual host. It costs
    whole minutes for nothing. These are one posting page per role, spread
    across employer domains and ATS hosts, so almost every consecutive pair is
    a different server and there was never a reason for them to queue.

    The database writes stay on the caller's thread. A sqlite connection may
    not be used from the thread that did not open it, and a scan that fetched
    faster but corrupted the roles table would not be an improvement.

    `pause` still works and still means what it said, for anyone who set it,
    and passing concurrency=1 restores the old behaviour exactly.
    """
    keys = keys or {}
    # Set on the first refusal of the Reed key. Every later Reed role in the
    # run would be refused for the same reason, so they are not asked: that
    # is the "stop after a failure" rule applied to a failure that is certain
    # to repeat, and it keeps a wrong key from costing one 401 per role.
    reed_refused = threading.Event()
    refusal: list = []

    def _try(chain, url, session) -> str:
        """The first fetcher in the chain that returns something.

        At most two requests for a role, and only when the second fetcher is
        a different one reading a different system: a role whose platform
        fetcher and URL fetcher are the same function is asked once.

        A failure that says why (a `Details` with `error`) is returned rather
        than flattened to "", so `run()` can report it.
        """
        last = ""
        for fn in chain:
            if fn is _from_reed:
                if reed_refused.is_set():
                    return refusal[0] if refusal else Details.make(
                        error="Reed refused the API key", refused=True)
                text = fn(url, session, api_key=keys.get("reed", ""))
                if getattr(text, "refused", False):
                    refusal[:1] = [text]
                    reed_refused.set()
            else:
                text = fn(url, session)
            if text:
                return text
            last = text if getattr(text, "error", "") else last
        return last

    fetchers = [(i, r, fetcher_for(r["url"], r["platform"]))
                for i, r in enumerate(rows, 1)]
    if concurrency <= 1:
        session = requests.Session()
        for i, r, chain in fetchers:
            yield i, r, (_try(chain, r["url"], session) if chain else "")
            if pause and i < len(rows):
                time.sleep(pause)
        return

    limiter = fetch_mod.HostLimiter()
    local = threading.local()

    def one(item):
        i, r, chain = item
        if not chain:
            return i, r, ""
        session = getattr(local, "session", None)
        if session is None:
            session = local.session = requests.Session()
        # Same pacing object the scan uses, so a posting page on
        # boards-api.greenhouse.io is queued behind the board listings rather
        # than racing them, and a host that has blocked us is not asked again.
        if limiter.blocked_for(r["url"]) > 0:
            return i, r, ""
        limiter.wait(r["url"])
        try:
            return i, r, _try(chain, r["url"], session)
        except Exception:
            # One unreadable posting must not end the pass. Before this ran in
            # a pool the loop had the same exposure, and a role with no text
            # passes every dealbreaker by default, so losing the rest of the
            # batch to one bad page is the expensive failure here.
            return i, r, ""

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        for i, r, text in ex.map(one, fetchers):
            yield i, r, text
