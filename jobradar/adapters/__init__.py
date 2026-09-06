"""Platform registry.

`REGISTRY` maps a platform name to how its URLs look, how to build one from a
token, and how to parse the response. Adding an ATS means adding one entry
here and one parser in `platforms.py`.

`verified` records whether the parser has been checked against live data from
that platform. Unverified ones are best-effort and are marked as such in the
README rather than quietly presented as equal.
"""

from __future__ import annotations

import re
import sys
import threading
from dataclasses import dataclass
from typing import Callable, Iterator

from ..models import Job, Source
from . import platforms


@dataclass
class Platform:
    name: str
    url_re: str
    parse: Callable[[object, Source], Iterator[Job]]
    build: Callable[[str], str] | None = None
    method: str = "GET"
    verified: bool = False
    note: str = ""

    def matches(self, url: str) -> bool:
        return bool(re.search(self.url_re, url, re.I))


def _wd_body() -> dict:
    return {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}


# --------------------------------------------------------------------------
# Composite tokens
#
# Most platforms address a board with one word, so `build` takes one word.
# Four do not: a Workday board is a tenant AND a datacentre number AND a site
# name, an Avature board is a host AND a path prefix. Those four had no
# `build` at all, which meant no way to turn a discovered address into a
# source, which meant every board on them had to be typed in by hand. In a
# list of 14,000 that left 3 Avature rows, 4 Phenom and 1 SuccessFactors, and
# Tesco was structurally unreachable until somebody added it manually.
#
# So the token carries the parts, separated by "|", exactly as
# `enumerate_boards.board_url` already spelled Workday. Anything that can
# print a token can now print a board.
# --------------------------------------------------------------------------

def _parts(token: str, n: int, *defaults: str) -> list[str]:
    """Split a composite token into exactly n pieces, padding from defaults.

    Padding rather than raising, because a token is often typed by hand and a
    missing trailing piece is nearly always the ordinary case: an Avature
    token with no site means the vendor's usual `careers`, a Workday token
    with no site means the tenant's `careers`. A short token that produced an
    exception instead would take the whole scan down over one row.
    """
    got = [p.strip() for p in token.split("|")]
    got += list(defaults)[len(got):]
    return (got + [""] * n)[:n]


def _host(tenant: str, vendor: str) -> str:
    """A tenant that already contains a dot is a hostname, not a subdomain.

    This is the whole reason Avature needed a host in the token. Tesco Bank
    are at tescoinsuranceandmoneyservices.avature.net, but Tesco themselves
    are at careers.tesco.com, which is Avature serving from the employer's own
    domain. A builder that only ever appended `.avature.net` could not express
    the second one, and Tesco was therefore unreachable.
    """
    return tenant if "." in tenant else f"{tenant}.{vendor}"


def build_workday(token: str) -> str:
    """`tenant|wdN|site` -> the cxs JSON endpoint.

    Lived in `enumerate_boards.board_url` as a special case. Moved here so the
    enumerator has no per-platform knowledge left and so `discover` and the
    harvester build the same URL from the same string.
    """
    tenant, ver, site = _parts(token, 3, "", "wd1", "careers")
    return (f"https://{tenant}.{ver}.myworkdayjobs.com"
            f"/wday/cxs/{tenant}/{site}/jobs")


def build_avature(token: str) -> str:
    """`host|path-prefix` -> the search page.

    The prefix may hold more than one segment: Tesco's is
    `en_GB/careersmarketplace`. It is never lowercased, because Avature paths
    are case-sensitive and `en_GB` is not `en_gb`.
    """
    tenant, site = _parts(token, 2, "", "careers")
    return (f"https://{_host(tenant, 'avature.net')}/{site.strip('/')}"
            f"/SearchJobs/?jobRecordsPerPage=50")


def build_rmk(token: str) -> str:
    """`tenant|prefix` -> the SuccessFactors search page.

    The prefix is optional: some tenants serve the board at `/search/` and
    some, Transport for London among them, at `/tfl/search/`. An empty prefix
    has to collapse rather than leave `//search/`, which 404s.
    """
    tenant, prefix = _parts(token, 2, "", "")
    path = f"/{prefix.strip('/')}" if prefix.strip("/") else ""
    return (f"https://{_host(tenant, 'jobs2web.com')}{path}"
            f"/search/?q=&sortColumn=referencedate&sortDirection=desc")


def build_taleo(token: str) -> str:
    """`tenant|section` -> the career section's job search page.

    Both halves are needed and neither can be guessed. A Taleo tenant runs
    several career sections and there is no default one: Hilton's is
    `us_hotel_ext`, Transport for London's is `external`, D.R. Horton's and
    TTEC's are both `2`, The College of New Jersey's is `00_ex_staff`. A
    section that does not exist answers 200 with "Career Section Unavailable"
    rather than 404, so guessing produces a page that looks fine and holds no
    jobs.

    This deliberately builds the human page rather than the JSON endpoint the
    page calls. The endpoint is addressed by a `portal` number that appears
    nowhere except inside the page, so no pure function could produce it, and
    `discover` reads its tokens off careers pages where only this form
    appears. `fetch_taleo` does the two-step.
    """
    tenant, section = _parts(token, 2, "", "")
    return (f"https://{_host(tenant, 'taleo.net')}"
            f"/careersection/{section.strip('/')}/jobsearch.ftl?lang=en")


def build_phenom(token: str) -> str:
    """`host|locale` -> the search-results page.

    Phenom exposes no tenant id anywhere: the employer's own careers host IS
    the address, which is why `discover` already treats the host as the token.
    The locale is a path segment pair and it varies (`gb/en` for Serco,
    `global/en` for Thales), so it travels in the token rather than being
    guessed at fetch time.
    """
    host, locale = _parts(token, 2, "", "gb/en")
    return f"https://{host}/{locale.strip('/')}/search-results?s=1"


REGISTRY: list[Platform] = [
    Platform(
        "greenhouse",
        r"boards-api\.greenhouse\.io|job-boards(?:\.eu)?\.greenhouse\.io",
        platforms.parse_greenhouse,
        build=lambda t: (
            f"https://boards-api.greenhouse.io/v1/boards/{t}/jobs"
            f"?content=true&pay_transparency=true"
        ),
        verified=True,
        note="pay_transparency=true is required for salary; content=true does not imply it",
    ),
    Platform(
        "ashby",
        r"api\.ashbyhq\.com/posting-api",
        platforms.parse_ashby,
        build=lambda t: (
            f"https://api.ashbyhq.com/posting-api/job-board/{t}"
            f"?includeCompensation=true"
        ),
        verified=True,
        note="200 + empty array for unknown tokens; validate on job count",
    ),
    Platform(
        "lever",
        r"api\.lever\.co/v0/postings",
        platforms.parse_lever,
        build=lambda t: f"https://api.lever.co/v0/postings/{t}?mode=json",
        verified=True,
        note="returns a bare top-level list; tokens are case-sensitive",
    ),
    # Lever runs two separate deployments and they do not share data. Every
    # European board answers 404 on api.lever.co and 200 on api.eu.lever.co,
    # with byte-identical JSON, which is why this shares parse_lever and only
    # differs in the host. Checked live: seb 98 postings, jacquemus 46,
    # innogames 3, all three 404 on the US host. Before this entry existed the
    # single hardcoded US builder made every EU board look dead, and
    # `validate --prune` deletes boards that look dead.
    #
    # A second registry entry rather than a token convention ("eu:seb") because
    # the registry keys on URL shape, and a different API host IS a different
    # URL shape: it needs its own url_re for `detect` and its own `build`. A
    # convention would have had to smuggle the region through the token, which
    # is the one field `discover` reads verbatim off a careers page.
    # `parse_lever` still stamps these jobs `platform="lever"`, because for
    # everything downstream of the fetch they are ordinary Lever postings.
    Platform(
        "lever_eu",
        r"api\.eu\.lever\.co/v0/postings",
        platforms.parse_lever,
        build=lambda t: f"https://api.eu.lever.co/v0/postings/{t}?mode=json",
        verified=True,
        note="Lever's EU deployment; identical shape, separate data. "
             "Tokens are case-sensitive: `Expana` is 200 and `expana` is 404",
    ),
    Platform(
        "workday",
        r"myworkdayjobs\.com/wday/cxs",
        platforms.parse_workday,
        build=build_workday,
        method="POST",
        verified=True,
        note="POST only; 406 (not 404) for unknown tenants due to wildcard DNS. "
             "The token is `tenant|wdN|site`: none of the three follow from "
             "the company name, so all three have to be carried",
    ),
    Platform(
        "workable",
        r"apply\.workable\.com/api",
        platforms.parse_workable,
        build=lambda t: (f"https://apply.workable.com/api/v1/widget/accounts/{t}?details=true"),
        verified=True,
    ),
    Platform(
        "smartrecruiters",
        r"api\.smartrecruiters\.com/v1/companies",
        platforms.parse_smartrecruiters,
        build=lambda t: f"https://api.smartrecruiters.com/v1/companies/{t}/postings?limit=100",
        verified=True,
        note="200 + empty content for unknown company ids",
    ),
    Platform(
        "linkedin",
        r"linkedin\.com/jobs-guest",
        platforms.parse_linkedin,
        verified=True,
        note="public guest endpoint, HTML cards, no description or salary. "
             "NOTE: LinkedIn's robots.txt is Disallow:/ for all agents, so this "
             "source is fetched in spite of it. See the README before using it.",
    ),
    # Workable twice: 2,094 employer boards on apply.workable.com, and this,
    # Workable's own aggregator over every board it hosts. A separate platform
    # NAME, because `parse` dispatches on the name and the two payloads are
    # different shapes. dedupe groups on employer and title rather than on
    # platform, so a role found both ways still meets itself; the separate
    # name is what lets `directness` give the employer's own board the win.
    # See parse_workable_search for why the search is worth having when the
    # boards are already on the list.
    # The whole of Workable, by the day it was posted, rather than 2,094
    # boards one at a time.
    #
    # `jobs.workable.com/api/v1/jobs?day_range=7` answers with every posting
    # created across every Workable employer in that window: 6,333 for a day
    # and 21,062 for a week, measured. At three requests a second that is 98
    # seconds for a day and under six minutes for a week, against the 49.9
    # minutes the 2,094 employer boards cost on a host that will not go above
    # 0.7 requests a second.
    #
    # It is the same payload shape as the keyword search, so it needs no
    # parser of its own.
    #
    # It does NOT replace those boards, and the difference is the point.
    # Workable lets an employer publish a role to their own careers page
    # without publishing it to jobs.workable.com, and an entire employer can
    # be hidden. Measured across 25 employers read both ways: 21 identical,
    # and one board of 61 roles came back as 25 here, with a Senior
    # Engineering Manager among the 36 missing. So this is the fast sweep and
    # the boards are the slow one that catches what it hides.
    Platform(
        "workable_recent",
        r"jobs\.workable\.com/api/v1/jobs\?day_range",
        platforms.parse_workable_search,
        build=lambda kw: (
            "https://jobs.workable.com/api/v1/jobs?day_range="
            + (kw if kw.isdigit() else "7")
        ),
        verified=True,
        note="every posting created across every Workable employer in the "
             "window, 21,062 in a week. Twenty a page behind the same opaque "
             "cursor as the keyword search, and walked to exhaustion rather "
             "than capped, because the cap is what would silently drop the "
             "tail of a sweep whose whole job is completeness",
    ),
    Platform(
        "workable_search",
        r"jobs\.workable\.com/api/v1/jobs",
        platforms.parse_workable_search,
        build=lambda kw: (
            "https://jobs.workable.com/api/v1/jobs?query="
            + kw.replace(" ", "%20")
        ),
        verified=True,
        note="the aggregator, not a board: one search reaches every Workable "
             "employer rather than the 2,094 a crawl happened to find, and "
             "carries the full description, so these roles need no enrichment "
             "pass. Twenty results a page behind an opaque cursor; `limit` is "
             "a 400 and every other page-size name is accepted and ignored",
    ),
    # And Workable a third way: one employer's board, read off the aggregator
    # host rather than the boards host. Registered under its own NAME because
    # `parse` dispatches on `src.platform` first, and under its own URL
    # pattern because `detect` has to tell it from the search, which lives one
    # path segment away on the same host. The postings it yields say
    # `workable_search`, which is deliberate and is explained in the parser.
    #
    # No `build`: the address takes Workable's account UUID and there is no
    # published route from a board slug to one. See the parser for what was
    # tried. A source using this has to have been given the UUID.
    Platform(
        "workable_company",
        r"jobs\.workable\.com/api/v1/companies/",
        platforms.parse_workable_company,
        verified=True,
        note="one employer's whole board on jobs.workable.com rather than "
             "apply.workable.com, which refuses 9.8% of a long run at the "
             "0.7/s it is paced at. Forty read back to back at 2.83/s drew no "
             "refusal, with a scan saturating apply at the same moment. Pages "
             "twenty at a time behind an opaque cursor, where the widget "
             "returns the whole board at once, and is addressed by account "
             "UUID, which a board slug does not yield",
    ),
    # Workable is the only one. Checked 2026-08-25, every claim below is a
    # real response and not a reading of the vendor's documentation, because
    # the documentation was wrong about SmartRecruiters in both directions.
    #
    # The prize was Greenhouse: 4,078 boards on this list against Workable's
    # 2,094. Greenhouse does run the same thing Workable does, MyGreenhouse
    # Jobs, a search across every board it hosts. It is behind a login.
    # my.greenhouse.io/jobs/search.json answers 401 {"error":"You need to sign
    # in or sign up before continuing."} and the HTML route 302s to
    # /users/sign_in. So the aggregator exists and cannot be read.
    #
    # The rest, in board-count order, with what they actually answered:
    #   ashby 2,607     jobs.ashbyhq.com/ is a 404 at the root; the only
    #                   cross-board surface is the GraphQL endpoint the boards
    #                   use, and introspection on it is disabled, so there is
    #                   no search operation to find. No sitemap either.
    #   icims, workday, oracle: not looked at here, employer-scoped by design.
    #   personio 1,258  jobs.personio.com and jobs.personio.de are NXDOMAIN.
    #                   There is no aggregator host to search.
    #   breezy 1,191    breezy.hr/jobs is a CloudFront 403 "Request blocked",
    #                   which is bot protection and is left alone.
    #                   jobs.breezy.hr is not a portal, it is an ordinary
    #                   tenant named "jobs" whose /json is an empty array.
    #   recruitee 993   jobs.recruitee.com looks like an aggregator and is
    #                   not: /api/offers/ is a 200 carrying ten Tellent
    #                   postings, i.e. Recruitee's own parent company's board.
    #                   Everything else on that host 302s to careers.tellent.com.
    #   smartrecruiters 910
    #                   The one the docs lie about. api.smartrecruiters.com
    #                   /jobs is documented with a `q` full-text search and
    #                   reads like a global one; it is 401 without a token and
    #                   the token is a company's, so it searches that
    #                   company's postings. The public Posting API is
    #                   /v1/companies/{id}/postings, which is what the
    #                   `smartrecruiters` entry above already fetches.
    #   jobvite 257     jobs.jobvite.com/search 302s to search.jobvite.com,
    #                   which 302s on to a marketing page. Nothing behind it.
    #   teamtailor 58   api.teamtailor.com/v1/jobs is 401 and the key is
    #                   per-tenant. The one feed that does span customers is
    #                   an XML feed handed out to approved job-board partners
    #                   by email, so it needs a business relationship.
    #   lever 25+40     api.lever.co/v0/postings with no company is 404
    #                   {"ok":false,"error":"Document not found"}.
    #   pinpoint 38     jobs.pinpointhq.com/ is a 404.
    #   bamboohr 1      jobs.bamboohr.com/ 302s to the marketing site.
    #   jazzhr 0        applytojob.com 301s to jazzhr.com.
    #
    # The practical consequence: the 4,078 Greenhouse boards and the 2,607
    # Ashby ones stay per-employer fetches. Only Workable can be collapsed.
    Platform(
        "nhs",
        r"jobs\.nhs\.uk/candidate/search",
        platforms.parse_nhs,
        build=lambda kw: (
            "https://www.jobs.nhs.uk/candidate/search/results?keyword="
            + kw.replace(" ", "+")
        ),
        verified=True,
        note="search page, not a board; JSON API is auth-gated and .rss returns HTML. "
             "Salary is usually stated because trusts publish Agenda for Change bands",
    ),
    # The only aggregator here reached through a documented, keyed API rather
    # than a public page. It is keyword-driven like NHS Jobs, so it ships as a
    # `keyword_template` source and `sources.expand_templates` turns it into
    # one search per title in `titles.include`.
    #
    # `postedByDirectEmployer=true` is in the builder on purpose. Reed carries
    # the same vacancy once per agency that has been given it, and the API
    # gives no per-result flag saying which kind of listing you are looking
    # at: it is a request filter or nothing. Filtering at the query is
    # therefore the only place duplicates can be cut before they reach the
    # pipeline. Drop the parameter if you do want agency listings.
    Platform(
        "reed",
        r"reed\.co\.uk/api/",
        platforms.parse_reed,
        build=lambda kw: (
            "https://www.reed.co.uk/api/1.0/search"
            f"?keywords={kw.replace(' ', '%20')}"
            "&postedByDirectEmployer=true"
        ),
        verified=False,
        note="needs a free API key as the HTTP Basic username, empty password. "
             "401 without one, and 200 with an empty `results` list both for a "
             "search that matched nothing and for a nonsense keyword, so "
             "liveness is the result count. Search results carry no "
             "`salaryType`, so an unlabelled figure below 2,000 is a rate and "
             "is left unconfirmed rather than read as an annual salary. "
             "UNVERIFIED against a live call: no key was obtainable here",
    ),
    # The second keyword-driven aggregator, and the only source here that can
    # watch more than one country from the same config: Adzuna runs nineteen
    # national indexes and the country is two letters in the path, so the
    # relocation countries are a copy of this line with `gb` swapped out.
    #
    # `title_only` rather than `what` on purpose. `what` searches the advert
    # body, which for "engineering manager" returns every engineer whose
    # advert mentions their manager, and the title filter downstream then
    # discards nearly all of it after we have spent the request. The free tier
    # is 250 calls a day, so a wasted page is not free.
    #
    # Nothing else is added to the query. Adzuna answers an unrecognised
    # parameter with a 400 and no results, which would take the whole source
    # down, so every parameter here is one that appears in Adzuna's own
    # OpenAPI description of the endpoint.
    Platform(
        "adzuna",
        r"api\.adzuna\.com/v1/api/jobs/",
        platforms.parse_adzuna,
        build=lambda kw: (
            "https://api.adzuna.com/v1/api/jobs/gb/search/1"
            f"?title_only={kw.replace(' ', '%20')}"
            "&results_per_page=50"
        ),
        verified=False,
        note="needs a free app_id and app_key, both in the query string: "
             "Adzuna offers no header auth, so the fetcher adds them per "
             "request and never writes them onto the stored source. Without "
             "them the answer is 400 with an HTML error page, not JSON. "
             "`salary_is_predicted` is '1' when the figure is Adzuna's own "
             "Jobsworth model rather than the employer, and those are left "
             "unconfirmed. The country is in the URL path and nowhere in the "
             "payload; descriptions are truncated to 500 characters by "
             "Adzuna; there is no remote field and no direct-employer filter. "
             "UNVERIFIED against a live call: no key was created here",
    ),
    Platform(
        "recruitee",
        r"\.recruitee\.com/api/offers",
        platforms.parse_recruitee,
        build=lambda t: f"https://{t}.recruitee.com/api/offers/",
    ),
    Platform(
        "breezy",
        r"\.breezy\.hr/json",
        platforms.parse_breezy,
        build=lambda t: f"https://{t}.breezy.hr/json",
        verified=True,
        note="bare top-level list at /json, like Lever; 200 + empty list for an "
             "unknown token, like Ashby; countries are ISO alpha-2 so the UK "
             "arrives as GB; the list has no description, `enrich` reads the "
             "posting page's schema.org JSON-LD for that",
    ),
    Platform(
        "jobvite",
        r"jobs\.jobvite\.com/[^/]+/jobs",
        platforms.parse_jobvite,
        build=lambda t: f"https://jobs.jobvite.com/{t}/jobs",
        verified=True,
        note="no public JSON at all: jobs.json, /search/jobs and jobs.rss all "
             "return the same career-site HTML. The list is server-rendered so "
             "no browser is needed. An unknown company 302s to a page with no "
             "rows, and the location cell says 'Hybrid Remote' for hybrid roles",
    ),
    Platform(
        "jazzhr",
        r"\.applytojob\.com/apply",
        platforms.parse_jazzhr,
        build=lambda t: f"https://{t}.applytojob.com/apply",
        verified=True,
        note="865 employer hosts in one Common Crawl index, the largest "
             "platform this tool could not read. The RSS feed at "
             "/apply/jobs.rss answers 410 Gone, so the server-rendered HTML "
             "list is the only route. The whole board arrives on one page: no "
             "page parameter, no offset, no total, which is the one case where "
             "a single response is not a truncation bug. It states the "
             "employer's own name in a schema.org Organization block, so "
             "identity here is evidence rather than an echo of our label",
    ),
    Platform(
        "taleo",
        # The host AND the path, because `taleo.net` alone would also match
        # Taleo's own marketing pages and the `/careersection/` path is what
        # says this is a board.
        r"taleo\.net/careersection/",
        platforms.parse_taleo,
        build=build_taleo,
        verified=True,
        note="255 employer hosts in one Common Crawl index. The board page is "
             "a JavaScript shell with no rows in it: `fetch_taleo` reads the "
             "JSON endpoint the page calls, which needs a `tz` request header "
             "or it answers 500, and no cookie, token or session of any kind. "
             "Token is `tenant|section`; a section that does not exist "
             "answers 200 with careerSectionUnAvailable. Pages in 25s on "
             "`pageNo`, ignores a pageSize we send while echoing it back, and "
             "serves the last page again forever past the end, so the stop "
             "condition is 'no new contest numbers' and there is a hard cap. "
             "The row columns are configured per career section and carry no "
             "headers, so nothing is read by position. Taleo states the "
             "employer's name in one place only, the RSS channel title",
    ),
    Platform(
        "bamboohr",
        r"\.bamboohr\.com/careers/list",
        platforms.parse_bamboohr,
        build=lambda t: f"https://{t}.bamboohr.com/careers/list",
        verified=True,
        note="summary index only: no description, no date, no salary, and no "
             "country for office or hybrid roles. `enrich` reads the advert "
             "from /careers/<id>/detail. An unknown subdomain answers 200 with "
             "BambooHR's marketing homepage, so status code proves nothing",
    ),
    Platform(
        "pinpoint",
        r"\.pinpointhq\.com/(?:[a-z]{2}/)?postings\.json",
        platforms.parse_pinpoint,
        build=lambda t: f"https://{t}.pinpointhq.com/postings.json",
        verified=True,
        note="/postings.json is the documented free endpoint; /jobs.json is "
             "deprecated and /api/v1/jobs is 401 without an X-API-KEY. "
             "Structured pay behind `compensation_visible`, and no posting "
             "date anywhere in the payload",
    ),
    Platform(
        "teamtailor",
        r"\.teamtailor\.com/jobs\.rss",
        platforms.parse_teamtailor,
        # per_page is honoured; the feed's own default is the first 100 only.
        build=lambda t: f"https://{t}.teamtailor.com/jobs.rss?per_page=200",
        verified=True,
        # This entry has to sit above the generic `rss` one, whose pattern
        # `\.(?:rss|xml)(?:$|\?)` matches this URL too. `detect` returns the
        # first match, so the order is what stops every Teamtailor board being
        # read by the generic feed parser, which knows nothing about
        # remoteStatus, tt:country or tt:department.
        note="/jobs.rss, not the /jobs.json feed: only the RSS carries "
             "remoteStatus, department and the country spelled out in full. "
             "404s honestly for an unknown subdomain, unlike Ashby and Breezy, "
             "but a live board with nothing open is still a 200 with no items",
    ),
    Platform(
        "personio",
        r"jobs\.personio\.(?:de|com)/xml",
        platforms.parse_personio,
        build=lambda t: f"https://{t}.jobs.personio.de/xml",
    ),
    Platform(
        "oracle",
        r"oraclecloud\.com/hcmRestApi/.*recruitingCEJobRequisitions",
        platforms.parse_oracle,
        # The token here is "<host>|<siteNumber>", because neither is
        # derivable from the company name: Marks and Spencer are on
        # fa-eqid-saasfaprod1.fa.ocs.oraclecloud.com.
        build=lambda tok: (
            lambda host, site: (
                f"https://{host}/hcmRestApi/resources/latest/"
                f"recruitingCEJobRequisitions?onlyData=true"
                f"&expand=requisitionList.secondaryLocations"
                f"&finder=findReqs;siteNumber={site},limit=200,"
                f"sortBy=POSTING_DATES_DESC"
            )
        )(*(tok.split("|") + ["CX_1"])[:2]),
        verified=True,
        note="postings nest at items[0].requisitionList; list view carries no salary",
    ),
    Platform(
        "phenom",
        r"/search-results|phenompeople",
        platforms.parse_phenom,
        build=build_phenom,
        verified=True,
        note="renders in the browser but embeds the first ten results as JSON "
             "in phApp.ddo; `fetch_phenom` uses the /widgets POST endpoint, "
             "which returns fifty at a time and a true total. The token is "
             "`host|locale`, because Phenom has no tenant id at all",
    ),
    Platform(
        "amazon",
        r"amazon\.jobs/.*search\.json",
        platforms.parse_amazon,
        # The token is the ISO alpha-3 country, because the board is scoped by
        # one. A worldwide read is capped at ten thousand by the API and is
        # therefore knowably incomplete; the UK is 785 and complete.
        build=lambda tok: (
            "https://www.amazon.jobs/en/search.json?base_query=&sort=recent"
            f"&result_limit=100&offset=0"
            + (f"&country={tok.upper()}" if tok else "")
        ),
        verified=True,
        note="their own board and their own API. A page is 100 and asking for "
             "more answers {\"hits\": 0, \"jobs\": null}, a clean 200 that "
             "reads as an empty board. `hits` caps at 10000 whatever the board "
             "holds, so an unscoped read is truncated by the API itself. "
             "Country is ISO alpha-3, which nothing downstream filters on, and "
             "the advert is split across description, basic_qualifications and "
             "preferred_qualifications: the must-haves are in the second one",
    ),
    Platform(
        "google_careers",
        r"/about/careers/applications/jobs/results",
        platforms.parse_google_careers,
        note="Google publish no ATS feed. The careers search is "
             "server-rendered and the whole result set sits in the page's own "
             "`ds:1` boot payload, so this reads structured data rather than "
             "rendered markup. Rows are positional, so the parser drops any "
             "row whose id and title do not look like an id and a title: a "
             "column that moves would otherwise store a URL as a job title. "
             "Alphabet companies share the board and keep their own names "
             "(DeepMind, Waymo, YouTube), so the row's company wins over the "
             "source's.",
    ),
    Platform(
        "pcsx",
        r"/api/pcsx/search",
        platforms.parse_pcsx,
        # The token is the host. PCSX keys the tenant off a `domain` query
        # parameter rather than the path, and it is not derivable from the
        # host: Microsoft serve from apply.careers.microsoft.com and identify
        # themselves as microsoft.com.
        build=lambda tok: (
            lambda host, domain: (
                f"https://{host}/api/pcsx/search"
                f"?domain={domain}&query=&location=&start=0"
            )
        )(*(tok.split("|")
            # Falling back to the last two labels, not to everything after the
            # first: apply.careers.microsoft.com is microsoft.com, and
            # `split(".", 1)[-1]` answers careers.microsoft.com, which the
            # endpoint accepts and answers with an empty board.
            + [".".join(tok.split("|")[0].split(".")[-2:])])[:2]),
        verified=True,
        note="Phenom's newer product and a different system from `phenom`: a "
             "plain GET returning data.positions, no phApp.ddo and no "
             "/widgets. Ten rows a page whatever `num` asks for, so a whole "
             "board is count/10 requests; Microsoft is 212. The list carries "
             "no advert text at all, so every role needs enrich",
    ),
    Platform(
        "rmk",
        r"jobs2web\.com/.*/search|/search/\?q=",
        platforms.parse_rmk,
        build=build_rmk,
        verified=True,
        note="SuccessFactors Recruiting Marketing; hrefs carry a tenant prefix. "
             "Token is `tenant|prefix`, prefix optional. Paginates on "
             "`startrow` in twenty-fives, and states no total anywhere",
    ),
    # The pattern is the PATH, not the host, and that is the whole point.
    # Avature runs boards on its own `<tenant>.avature.net` and equally often
    # on the employer's domain, and the second kind is invisible to a
    # host-based signature: Tesco's board is careers.tesco.com, which has
    # nothing in it to match on. `/SearchJobs/?jobRecordsPerPage=` is Avature's
    # own query shape and no other platform here emits it, so it is specific
    # enough to be safe: a bare `/SearchJobs/` on an unrelated site does not
    # match, which matters because plenty of sites have a page by that name.
    Platform(
        "avature",
        r"avature\.net/.*SearchJobs|/SearchJobs/?\?[^ ]*jobRecordsPerPage=",
        platforms.parse_avature,
        build=build_avature,
        verified=True,
        note="absolute hrefs to /JobDetail/; location lives in the slug. Token "
             "is `host|path-prefix`. Paginates on `jobOffset` and the page "
             "size is the tenant's, not ours: Tesco answers ten however many "
             "we ask for. `semanticSearch=` is a real server-side keyword "
             "filter, which is what keeps a 999+ board to a few requests",
    ),
    Platform(
        "icims",
        r"icims\.com/jobs/search",
        platforms.parse_icims,
        build=lambda t: f"https://{t}.icims.com/jobs/search?ss=1&in_iframe=1",
        verified=True,
        note="the plain search page is an empty shell; in_iframe=1 returns the real list",
    ),
    Platform(
        "rss",
        r"\.(?:rss|xml)(?:$|\?)|/rss/|successfactors|avature",
        platforms.parse_rss,
    ),
]

_FALLBACK = Platform("custom", r".*", platforms.parse_generic)


def detect(url: str) -> Platform:
    for p in REGISTRY:
        if p.matches(url):
            return p
    return _FALLBACK


def by_name(name: str) -> Platform | None:
    return next((p for p in REGISTRY if p.name == name), None)


# apply.workable.com's widget answers with the advert attached only when it is
# asked to. Without `details=true` the response carries no description at all,
# and 2,094 boards, fifty minutes and the slowest phase of the whole scan were
# producing roles that could not be ranked (rank refuses anything under 200
# characters), could not be checked against a dealbreaker, and had no salary,
# because `parse_text` was reading an empty string. 219 of 219 stored Workable
# roles had no description on 2026-08-27.
#
# It costs no extra request: same URL, same host, same pacing floor. Measured
# over five real boards it is 11.5x the bytes and 1.28x the wall time, about
# two and a half minutes across the phase, against fifty minutes that were
# already being spent to fetch titles alone.
#
# Normalised here rather than in the bundled file alone, so an existing
# sources.json and anything in `extra_sources` are fixed too. Somebody who
# already has the list does not have to re-download it to get descriptions.
_WORKABLE_WIDGET = re.compile(
    r"^https?://apply\.workable\.com/api/v\d+/widget/accounts/[^/?#]+/?$")


def _want_details(url: str) -> str:
    if not url or not _WORKABLE_WIDGET.match(url.split("?")[0]):
        return url
    if "details=true" in url:
        return url
    return url.rstrip("/") + "?details=true"


def prepare(src: Source) -> Source:
    """Fill in platform and POST body from the URL shape."""
    src.url = _want_details(src.url)
    p = detect(src.url)
    if not src.platform:
        src.platform = p.name
    if p.method == "POST":
        # Remember that these were worked out from the URL, so writing the
        # list back out does not bake in something this function re-derives on
        # every load.
        src.derived_request = src.method != "POST" or not src.body
        src.method = "POST"
        if not src.body:
            src.body = _wd_body()
    return src


# Every board this run could not read, in the order it happened: one
# (company, platform, reason) per failure. A scan reads its own list back out
# to say how many of its zeros were "no vacancies" and how many were "we could
# not read it"; a test reads it to prove the difference was recorded at all.
# Appended under a lock because the scan parses on sixteen worker threads.
_unreadable: list[tuple[str, str, str]] = []
_unreadable_lock = threading.Lock()

# How many of them get a line of their own on stderr before this goes quiet.
# The list keeps every one; the printing stops, because a platform that breaks
# for all 4,078 of its boards would otherwise bury the rest of the scan's
# output under 4,078 identical lines and the reader would lose the summary
# that matters. The last line says how many were not printed.
MAX_UNREADABLE_LINES = 20


def unreadable() -> list[tuple[str, str, str]]:
    """The boards `parse` could not read since `clear_unreadable`."""
    with _unreadable_lock:
        return list(_unreadable)


def clear_unreadable() -> None:
    with _unreadable_lock:
        _unreadable.clear()


def parse(payload, src: Source) -> list[Job]:
    """Parse one board's payload, or record why not and return nothing.

    Returning `[]` is still right: one malformed board must not end a run over
    17,810 sources. Returning it SILENTLY is not, and that was the whole of
    this function until now. A board that answers HTTP 200 with a holding
    page, a login wall or a vendor error page raises inside its platform's
    parser, the exception was swallowed here, and the source arrived at
    `cmd_scan` as `counts[key] = 0` -- indistinguishable from an employer with
    no vacancies, and counted in the line that says "N sources responded with
    no postings at all".

    `discover._parse_or_why` fixed exactly this for `validate` in 2715264,
    because `validate --prune` deletes what it reads as dead. The scan path
    was left as it was, so the same payload still produced the right answer
    through one command and a wrong one through the other. This closes that:
    the list is kept, and the failure is said out loud once, on stderr, where
    a scan's own output is.

    `KeyboardInterrupt` is gone from the handler with no change in behaviour.
    It is a `BaseException`, so `except Exception` never caught it, and both
    arms of the conditional returned the same empty list anyway.
    """
    p = by_name(src.platform) or detect(src.url)
    try:
        jobs = [j for j in p.parse(payload, src) if j.title and j.url]
        # What the source's own query already asserted, applied centrally so
        # every platform gets it rather than each parser remembering to.
        #
        # Only into a gap. A per-result field the platform actually sent is
        # better evidence than a filter on the URL, so an adapter that set
        # `employment` from the payload keeps it. See `Source.employment` for
        # why this exists and what has to be true before a source may declare
        # one.
        if src.employment:
            from ..employment import from_platform
            declared = from_platform(src.employment)
            if declared:
                for j in jobs:
                    if not j.employment or j.employment == "unstated":
                        j.employment = declared
        return jobs
    except Exception as e:  # a malformed board must not kill the whole run
        why = f"{type(e).__name__}: {str(e)[:200]}"
        with _unreadable_lock:
            _unreadable.append((src.company, src.platform or p.name, why))
            said = len(_unreadable)
        if said <= MAX_UNREADABLE_LINES:
            print(f"  ! {src.company}: the board answered, but its "
                  f"{src.platform or p.name} response could not be read "
                  f"({why}). That is not the same as having no postings.",
                  file=sys.stderr)
        elif said == MAX_UNREADABLE_LINES + 1:
            print(f"  ! more boards answered with something that could not "
                  f"be read; the rest are counted rather than listed. "
                  f"`adapters.unreadable()` has all of them.",
                  file=sys.stderr)
        return []


def platform_names() -> list[str]:
    return [p.name for p in REGISTRY]
