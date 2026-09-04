# Platform notes

[job-radar](../README.md)

Twenty-three board platforms, and what each one does that a reasonable client
gets wrong. Each of these cost a debugging session, and they are the reason
the adapters are shaped the way they are. Cornerstone OnDemand has a row here
despite being unreadable, because the reason is worth writing down.

**What this file does and does not cover.** The code carries 32 adapters, and
these rows account for 23 of them, the Lever row covering both its
deployments. Nineteen of the rows are platforms the bundled source list
actually uses; JazzHR, Reed and Adzuna have a row and no entry on it, and
Cornerstone has a row and no adapter. Four platforms the list does use have no
row here, three of them large: **Workable's own boards** (2,094 sources),
**Personio** (1,258), **Recruitee** (992) and Workable's recently-posted feed
(1). Personio and Recruitee are two of the five adapters marked unverified,
and the closing paragraph says what that means. Workable's own boards are
neither unverified nor unread, and their absence is a gap in this file rather
than in the code.

One shape recurs often enough to name before the table: **failure here usually
looks like success.** Ashby answers HTTP 200 with an empty array both for a
board that does not exist and for one that is rate-limiting you. Workday
answers 406 rather than 404 for a tenant that is not there, because of
wildcard DNS. Taleo echoes back the page size it ignored, and serves the last
page again forever rather than an empty one, so a loop stopping on an empty
page never stops. So nothing here validates on a status code, and every pager
stops on evidence rather than on absence.

| Platform | What bites |
|---|---|
| **Greenhouse** | Returns **403 if you attach a body to a GET**. Salary needs `?pay_transparency=true`; `content=true` is a separate parameter and does not imply it. |
| **Ashby** | Returns **HTTP 200 with an empty array** for a token that does not exist *and* for one being rate-limited. Validate on job count, never status code. Compensation needs `?includeCompensation=true`. |
| **SmartRecruiters** | Same empty-200 behaviour as Ashby. |
| **Workday** | **POST**, not GET. Returns **406, not 404**, for a tenant that does not exist, because of wildcard DNS on `*.myworkdayjobs.com`. A non-404 proves nothing. Tenant and site names cannot be guessed: 117 attempts produced zero working tenants, so the token carries all three parts it needs, `tenant|wdN|site`, as in John Lewis's `jlp|wd3|JLPjobs_careers`. |
| **Lever** | Returns a bare top-level list rather than an object with a `jobs` key. **Two deployments that do not share data**: `api.lever.co` for the US and `api.eu.lever.co` for Europe, identical JSON, and a board on one answers **404** on the other. Checked live: `seb` 98 postings, `jacquemus` 46, `innogames` 3, all three 404 on the US host. **Tokens are case-sensitive** (`Expana` is 200, `expana` is 404), so nothing may lowercase one. Worth knowing why the EU side is where the boards turn up: `jobs.lever.co/robots.txt` disallows CCBot, ClaudeBot and GPTBot, `jobs.eu.lever.co/robots.txt` allows everything. |
| **Breezy HR** | The board is `https://<company>.breezy.hr/json`. Bare top-level list like Lever, and empty-200 for an unknown token like Ashby. Countries come back as **ISO alpha-2, so the UK is `GB`**, which is not the code the rest of the tool filters on. `is_remote` is **true for hybrid postings too**; `location.remote_details.value` is the field that tells them apart. The list carries **no description at all**, so `enrich` reads the posting page's schema.org JSON-LD instead. It does carry a formatted salary string: 24 of 110 live roles on one board stated pay. |
| **Jobvite** | **No public JSON at all.** `/<company>/jobs.json`, `/search/jobs` and `/jobs.rss` every one return the same career-site HTML, and `api/v1/jobs` redirects away. The list is server-rendered though, so no browser is needed. The markup is employer-customisable and does differ: NinjaOne ship `<td class="jv-job-list-name">` and LHH ship `<div>` for the same cell, so the **class names are the anchor, not the element**. The location cell carries the working arrangement in front of the place, and **"Hybrid Remote" contains the word "remote"**: reading it as a keyword marks all 31 hybrid roles on one board as remote. An unknown company **302s** and lands on a page with no rows, so a redirect-following fetch turns "no such board" into an ordinary 200. No advert text, date or salary in the list, so `enrich` reads the posting page's schema.org JSON-LD, the same block Breezy publishes. |
| **JazzHR** | 865 employer hosts in one Common Crawl index, the largest platform this tool could not read. The board is `https://<company>.applytojob.com/apply`, server-rendered, and **`/apply/jobs.rss` answers 410 Gone**, so the HTML list is the only route. The whole board arrives on one page: no page parameter, no offset, no total anywhere in the markup, which is the one case here where reading a single response is not a truncation bug. Unusually, the page **states the employer's own name** in a schema.org `Organization` block, so identity is evidence rather than an echo of our label. No advert text in the list, and the posting page has no `JobPosting` JSON-LD either, so `enrich` reads `<div id="job-description">`. |
| **Oracle Taleo** | 255 employer hosts in one Common Crawl index, the largest readable gap left after JazzHR. The board is `https://<tenant>.taleo.net/careersection/<section>/jobsearch.ftl` and the token is composite, `tenant|section`, because a tenant runs several career sections and there is no default one: Hilton's is `us_hotel_ext`, Transport for London's is `external`, TTEC's and D.R. Horton's are both `2`. **The page is a JavaScript shell with no job rows in it at all**, so the reader calls the JSON endpoint the page itself calls, `/careersection/rest/jobboard/searchjobs?lang=en&portal=<n>`. That endpoint needs **a `tz` request header or it answers HTTP 500** with the body "An Error Occurred in TEE"; the value is not checked, `tz: x` works. It needs nothing else: no cookie, no session, no CSRF token, no referer, no browser user agent. The `portal` number appears nowhere but inside the page, so reading a board is a two-step, and it is per-tenant rather than unique: BAE Systems and D.R. Horton are both on portal `101430233` and return 159 and 578 different postings. **The RSS feed is a trap.** `/careersection/feed/joblist.rss` exists and serves **at most 11 items whatever the board holds** (11 of TTEC's 116, 11 of D.R. Horton's 578), and answers a board with nothing open with one placeholder item titled "Unable to Create an RSS Feed". Its channel title is worth having though, because it is **the only place on the platform where Taleo states the employer's own name**: both `<title>` tags on an unbranded board read "Job Search", and The College of New Jersey's markup does not contain the words "College of New Jersey" anywhere. **Paging lies twice.** A `pageSize` we send is ignored and echoed back (ask for 100, get 25 under a stated pageSize of 100), and a page past the end returns the last page again rather than nothing, so a loop stopping on an empty page never stops. `totalCount` overstates too: TfL reports 3 and serves 1. The stop condition is "no new contest numbers" with a hard cap of six pages per search term. **The row columns are configured per career section and the JSON has no header row**, so nothing may be read by position: BAE Systems ship one column (title only, no location anywhere), TfL two (title, date), TTEC and D.R. Horton three. `linkedColumn` and `locationsColumns` are Taleo's own pointers and are what the parser trusts; the date is found by trying to parse whatever is left, because the format differs too ("Aug 24, 2026" against TfL's "13-Aug-26"). The location cell is a **JSON array serialised into a string** and is a hyphen-joined hierarchy, biggest first (`PH-National Capital-Quezon City, Metro Manila`), which the country matcher cannot read at all; the adapter reverses it and re-commas it. A leading two-letter code is expanded to a country **only when it is not also a US state abbreviation**, because D.R. Horton publish `IN-Indianapolis`, `AL-Spanish Fort` and `KY-Louisville` next to `Nebraska-Omaha` on one American board. **No working-arrangement field exists anywhere**, in the row or the facets, so remote is read from the words and a title saying hybrid is answered "not remote". No salary in any of the seven live career sections checked, and no department. `enrich` reads the advert from the posting page, where it is one URL-encoded entry in an `api.fillList(... 'descRequisition', ...)` array whose index moves per career section (10 on TTEC, 11 on BAE), so it takes the longest entry rather than a fixed one. **Known gap:** the older, pre-faceted career sections have no portal number and render their own rows from a positional array with no column names (Cook County, EFSA). Those are reported as unreadable by name rather than as empty. |
| **Cornerstone OnDemand** *(not read, and will not be)* | 589 employer hosts, and none of them reachable honestly. The careers page at `https://<company>.csod.com/ux/ats/careersite/<n>/home?c=<company>` answers **200**, but it is an empty single-page-app shell: 5.5KB with zero job data in it. The jobs come from `services/x/career-site/v1/...`, and every path under it answers **401 with the body `no Authorization header found`**, to a GET and to a POST alike. The page mints that token at runtime and hands it to its own JavaScript, so the only route in is lifting the token back out of the page. That is not a published API, it is working around an authentication check the vendor put there deliberately, and it is a different thing from ignoring robots.txt. So this is a dead end rather than an unwritten adapter, and `discover` names Cornerstone when an employer is on it rather than reporting "nothing found". |
| **BambooHR** | `/careers/list` is a **summary index, not a board**: no advert text, no apply URL, no date and no salary. `enrich` reads the advert from `/careers/<id>/detail`, which is the same JSON API the board itself runs on. An unknown subdomain answers **200 with BambooHR's own marketing homepage as HTML**, so neither the status code nor the content type proves a board exists. The field called **`isRemote` is a decoy**: it was null on all 155 postings across five live boards. `locationType` is the real one, pinned against the labels their `/jobs/embed2.php` widget renders for the same posting ids: `0` in-office, `1` remote, `2` hybrid. **Known gap:** the list gives no country for office and hybrid roles, only a city and a region, so a role in a town the location matcher does not know ("Farnborough") reaches the country filter unresolved and a country-filtered search drops it. The country is in the detail record, which `enrich` reads, but `enrich` only ever writes the description and the pay. |
| **Pinpoint** | `/postings.json` is the documented free endpoint. `/jobs.json` still answers but is **deprecated**, and `/api/v1/jobs` is **401 without an X-API-KEY**. Pay arrives as real numbers behind `compensation_visible`, which is the employer's own switch and must be obeyed: 131 of 179 roles on one board published a figure. `workplace_type` (`remote` / `hybrid` / `onsite`) separates remote from hybrid. **There is no posting date anywhere in the payload**, so these roles score flat on recency; the `/jobs.rss` feed has a `<pubDate>` but nothing else worth having. **No country either**, so the location is built from `city` + the spelled-out `province` and the country is left to be inferred. The advert is split across four fields, so reading only `description` throws away the responsibilities and must-haves. |
| **Teamtailor** | Two public feeds per career site and they are **not** equivalent. `/jobs.json` states the country as ISO alpha-2 (`GB`) and carries no remote flag and no department; **`/jobs.rss` carries the same descriptions plus `<remoteStatus>`, `<tt:department>` and `<tt:country>` spelled out in full**, so the RSS is the one to read. `remoteStatus` is `fully` / `hybrid` / `temporary` / `none`, which is the field that keeps hybrid roles out of a remote filter: 14 of Teamtailor's own 16 roles are `hybrid`. The feed returns the **first 100 jobs by default** and honours `?per_page=`. Unlike Ashby and Breezy it **404s honestly** for a subdomain that does not exist, but a live board with nothing open still answers 200 with no items. No salary field anywhere. |
| **LinkedIn** | The public `jobs-guest` endpoint returns server-rendered HTML cards to a plain GET, no login and no JavaScript. No description or salary, so those roles are leads rather than screenable postings. |
| **Phenom** | Renders in the browser, but the search page embeds only the first ten results as JSON under `phApp.ddo`. The site is really driven by a `/widgets` POST endpoint returning fifty at a time with a true total, which is the one worth using: Serco publish 359 roles. Phenom exposes no tenant id at all, so the employer's own careers host is the address and the token is `host|locale` (`careers.serco.com|gb/en`), the locale varying by employer. |
| **SuccessFactors RMK** | Still served from `jobs2web.com` hostnames. Server-rendered, so no browser needed, but hrefs carry a tenant prefix (`/tfl/job/...`) rather than a bare `/job/`, and the location sits in the URL slug ahead of the title rather than in its own field. The token is `tenant|prefix` with the prefix optional (`london-gov|tfl`), because some tenants serve the board at `/search/` and some at `/<prefix>/search/`. It pages on `startrow` in twenty-fives and states no total anywhere. The list carries no advert text, and the posting page publishes **no schema.org JSON-LD at all**, so `enrich` reads `<span class="jobdescription">` instead. Most of those spans nest further spans, so the closing tag is found by counting rather than by a lazy match: PSEG's advert is 15,758 characters and a lazy `(.*?)</span>` returns 121 of them, Hikma's 1,053 of 2,375. Some tenants serve `<span itemprop="description" class="jobdescription">`, so the class is matched as a word in the tag rather than as the whole attribute. |
| **iCIMS** | The plain search page is an empty shell that renders into an iframe. Adding **`in_iframe=1`** returns the server-rendered list. That single parameter is the whole difference between "no jobs" and a working board. **The posting page does the same thing**: the bare URL answers 200 with a 3.8KB shell containing no advert and no JSON-LD, so `enrich` asks for `in_iframe=1` there too and then reads the schema.org `JobPosting` block the iframe view carries. |
| **Avature** | Absolute hrefs to `/JobDetail/`, and the location is only in the slug. **The signature is the path, not the host**, because Avature serves as often from the employer's own domain as from `avature.net`: Tesco's board is `careers.tesco.com`, which has nothing in the hostname to match on. So the token is `host|path-prefix` and the prefix can be more than one segment (`en_GB/careersmarketplace`). It pages on `jobOffset`, and **the page size is the tenant's, not yours**: Tesco answers ten however many you ask for. `semanticSearch=` is a real server-side keyword filter, which is what keeps a 999+ board down to a few requests. The list carries no advert. **Whether the posting page publishes schema.org JSON-LD is per tenant**: Tesco's does and EA's does not, and both are ordinary Avature installs, so `enrich` tries that block first and falls back to Avature's own `article__content__view__field__value` divs. It takes every field block rather than the one under the description heading, because that heading is localised. A minority of tenants answer the posting page with **403** (`baufest.avature.net`, `avature.cn`, `portal.fritolayemployment.com`); those roles stay unenriched. |
| **Oracle Recruiting Cloud** | Postings nest at `items[0].requisitionList`, one level deeper than most, and `TotalJobsCount` is the real total rather than the page length. The host bears no relation to the company: Marks and Spencer are on `fa-eqid-saasfaprod1.fa.ocs.oraclecloud.com`. The list view carries no salary, so roles from here read as unconfirmed by nature rather than by parse failure. The list's `ShortDescriptionStr` is a teaser, not the advert, and **the posting page is a 4.4KB JavaScript shell** with no JSON-LD, so `enrich` calls `recruitingCEJobRequisitionDetails` instead: **plural**, because the singular spelling answers 404 with an empty body, which is indistinguishable from a dead board. The advert is split across `ExternalDescriptionStr`, `ExternalResponsibilitiesStr` and `ExternalQualificationsStr` and which are filled varies by tenant, so all of them are read and then deduplicated: one measured board had all three holding the same advert. The `ShortDescriptionStr` teaser is long enough to clear the ordinary 200 character re-read floor, which is how 185 of 483 roles kept a teaser: Oracle and Phenom carry a **1,200 character floor** of their own instead, set above every teaser measured and below every real advert. Fetching 20 of those returned a median 6,400 characters, every one between 3.8x and 16.2x what was stored. |
| **NHS Jobs** | The JSON API at `/api/v1/search_json` is behind an auth token, and the `.rss` path returns HTML rather than a feed, so the search page is the route. Ten results per page, no page-size parameter. Worth it: NHS trusts publish Agenda for Change bands, so **46 of 50 roles stated a salary** against a market average near a third. |
| **Reed** | One of the two sources that need a credential: a free API key, sent as the **HTTP Basic username with an empty password**, which is Reed's own documented scheme. Unkeyed requests are **401**, which is the good news, because a 401 cannot be mistaken for "no jobs today". A search that matched nothing is **200 with an empty `results` list**, and so is a nonsense keyword, so liveness is the result count. Pages are capped at **100** and walked with `resultsToSkip`. The catch is salary: the **search endpoint returns `minimumSalary` / `maximumSalary` with no period at all** (only the per-job details endpoint carries `salaryType`), so a bare `650` could be a year or a day. Anything under 2,000 is read as an unlabelled rate and left **unconfirmed** rather than annualised wrongly, and the advert text gets a second go at it. `locationName` is free text, so most of it is towns and counties the location matcher has never heard of ("Stoke-on-Trent", "Cambridgeshire"), and the country has to be added by the adapter or a UK-filtered search drops the lot; that append only skips a string that already names a country, so a bare town matching a foreign city ("Perth", "Boston") is filed British anyway. There is **no remote field**, and `employerName` is whoever posted the job, which on an agency listing is the agency. |
| **Workable search** (`jobs.workable.com`) | Workable's own search across every employer it hosts, rather than a board, and it needs no key. **Twenty results a page behind an opaque `nextPageToken`**, so the walk is strictly sequential and cannot be parallelised; `limit=100` is a **400**, and `pageSize`, `size`, `per_page` and `page_size` are all accepted and all ignored. Fifteen pages is the cap, 300 postings per title, and a scan says so when it bites rather than quietly returning the first 300. It carries the full advert, so these roles need no `enrich` pass. `{keyword}` and `{country}` are both narrowed at the query: "software engineer" worldwide is 4,220 postings over 211 pages, and the same search in the United Kingdom is 322. Deliberately a different host from `apply.workable.com`, so the 0.7/s pacing those 2,094 boards need does not throttle this and a block on one does not silently take out the other. Scored as an aggregator, so the employer's own board wins the row: 36% of what it finds is an employer already on the list. |
| **Adzuna** | Needs a free `app_id` and `app_key`, and both go in the **query string**: there is no header auth, so the fetcher adds them per request and never writes them onto the stored source or into the state file. Unkeyed requests answer **400 with an HTML error page**, not a 401 and not JSON. **The country is in the URL path** (`/v1/api/jobs/gb/search/1`) and appears nowhere in the payload, so the adapter reads it from there and names it in the location, or a UK filter drops every listing whose town is not in the city list; the same append-only-if-unnamed rule as Reed applies, so a bare town matching a foreign city is filed under the index's own country. Same for the **currency**, which follows the index. The trap is **`salary_is_predicted`**: `"1"` means the figure came from Adzuna's Jobsworth model rather than the employer, and treating a model output as a stated salary drops real roles against the floor and promotes ones that pay nothing like it, so those stay unconfirmed. **Descriptions are truncated to 500 characters** by documentation. **The page number is in the path, not a parameter**, and `results_per_page` is a request rather than a promise, so paging stops on an empty page and never on a short one. No remote field, and no direct-employer filter of any kind. |

**Which system publishes the advert is not always which board it came off.**
So `enrich` tries the platform's own reader first and then falls back to the
shape of the role's own URL. A Phenom `applyUrl` is the employer's real
applicant tracking system: of 1,882 Phenom roles measured, 1,562 point at a
Workday tenant, 73 at iCIMS and 33 at a SuccessFactors host, and both `custom`
boards on the list hand back iCIMS posting URLs. A URL matching none of the
twelve known shapes is left alone, exactly as before.

**Five adapters are unverified, and this table should not imply otherwise.**
The registry marks Recruitee, Personio and the generic RSS reader as
best-effort, which is why they have no row above: they parse, and they have
never been checked against live data.
Reed and Adzuna are a stronger caveat. Everything written about them above
comes from their published documentation and from their unkeyed error
responses, because no key was ever obtained here, so **neither adapter has
made a successful keyed call**. Treat your first run of either as the test,
and if it does not behave as described here, believe the run.

**Tokens rarely match company names.** `mymoose` is Rapid7. `evergreenix` is
Garrison. `knowbe4` is Egress. `primer.io` is Primer. This is why `discover`
reads them off the careers page instead of guessing, and why anything it does
guess is identity-checked before being offered.

---


## Platforms this cannot read

**Whole sectors sit on platforms with no adapter.** An early harvest of UK
employers resolved 34 boards from 196 attempted, and healthcare resolved
**zero of 25**, because NHS trusts use NHS Jobs rather than any commercial
applicant tracking system. That one is fixed: there is an NHS Jobs adapter,
which also picks up the private providers who advertise there. SuccessFactors
and Oracle have adapters now too. What is left unreadable is TRAC, Civil
Service Jobs, Eploy, Hireserve, Jobtrain, Networx, Oleeo, Oracle EBS
iRecruitment and CharityJob. Charity and public-sector recruitment runs
largely on those, and a fundraiser trying twenty-one organisations resolved
one. `discover` recognises all of them except TRAC and tells you which one an
employer uses, with the working board URL, rather than saying "nothing found".
An adapter for any of them would do more for coverage than another hundred
technology companies.

Civil Service Jobs deserves its own note, because it is the largest single
employer board in the UK and there is no useful coverage of UK public policy
roles without it. It is not merely unwritten: the search page sits behind a
bot interstitial and answers a plain request with "Quick check needed", so an
adapter is real work rather than an afternoon. `discover` names it when an
individual department points at it.

**Cornerstone OnDemand, which is a dead end rather than a gap.** 589 employer
hosts, and this will not read any of them. The careers page at
`https://<company>.csod.com/ux/ats/careersite/<n>/home?c=<company>` answers
200, but it is a 5.5KB single-page-app shell with no job data in it at all.
The jobs come from `services/x/career-site/v1/...`, and every path under that
answers **401 with the body `no Authorization header found`**, to a GET and to
a POST alike. The page mints the token at runtime and hands it to its own
JavaScript, so the only way in is to lift that token back out of the page.
That is not a published API. It is working around an authentication check the
vendor put there on purpose, which is a different thing from ignoring
robots.txt, and it is not something this tool does. `discover` names
Cornerstone when an employer is on it, so you get a diagnosis and the board
URL to open yourself rather than "nothing found". If Cornerstone ever
publishes an unauthenticated feed, that becomes an adapter; until then this is
the honest answer.

**Taleo's older career sections.** The Taleo adapter reads the faceted career
sections, which is where the volume is. The older generation renders its own
rows server-side from a positional array with no column names in it, has no
portal number, and cannot be read by the same parser. Cook County and EFSA are
both on that generation. Those boards are reported as unreadable by name
rather than counted as empty, which matters because `validate --prune` deletes
what reads as dead.

## How fast each platform is asked, and where that number came from

Politeness here is a per-host rate, not a global one, because the work is
bimodal. Seven hosts carry more than half the bundled list between them and
about 7,749 hosts carry one board each. A single concurrency number cannot
serve both: set it low enough for the seven and it wastes an hour on the long
tail, set it high enough for the tail and it becomes a burst against
Greenhouse. `HostLimiter` in `jobradar/fetch.py` gives every host its own
clock, `PER_HOST_RPS` holds the exceptions and `DEFAULT_PER_HOST_RPS` is what
everything else gets.

**The rate is only consulted on a host that is asked twice.** That is the fact
that makes this table short. A gap delays the SECOND request to a host, so on
the ~7,749 single-board hosts the default rate is a number that is never read,
and those boards are limited by `fetch.concurrency` and nothing else. Raising
the default rate does nothing for them; raising concurrency does nothing for
the seven. `job-radar scan` now says which of the two it is bound by before it
starts, and `jobradar.cli.pacing_floors` is the arithmetic behind that line.

### What each busy host is documented to allow

Checked 2026-08-27, by reading each vendor's own developer documentation.
**The honest summary is that for the public feeds this tool actually reads,
almost nothing is documented.** The published numbers that do exist are
attached to the vendors' AUTHENTICATED APIs, and none of those pages says the
limit extends to the anonymous endpoint.

| Host | Boards | Documented limit for the endpoint this tool calls | Where that comes from |
|---|---|---|---|
| `boards-api.greenhouse.io` | 4,078 | **None.** The Job Board API page states no rate limit, no 429 contract and no `Retry-After` behaviour anywhere on it. | [Job Board API](https://docs.greenhouse.io/job-board.html). The limit people quote is [Harvest's](https://docs.greenhouse.io/harvest.html), which is scoped in the text to "Harvest API requests" and never mentions the Job Board API. [Newer Harvest docs](https://harvestdocs.greenhouse.io/docs/api-rate-limiting) decline to publish an absolute number at all, so the widely repeated "50 per 10 seconds" is an example header value, not a documented allowance. |
| `api.ashbyhq.com` | 2,607 | **None** for `/posting-api/job-board/{name}`, and none for the authenticated posting API either. | [Public job posting API](https://developers.ashbyhq.com/docs/public-job-posting-api). The only number anywhere in Ashby's docs is 15 requests a minute for [`report.generate`](https://developers.ashbyhq.com/reference/reportgenerate), scoped per organisation, and generalising it to postings would be inventing a limit rather than finding one. |
| `api.smartrecruiters.com` | 910 | **10 requests a second**, 8 concurrent, for most endpoints. The Posting API is not on the 2/s list. | [Throttling policies](https://developers.smartrecruiters.com/docs/throttling-policies). The caveat is real: "All throttling policies are applied to an individual Customer API user", so an unauthenticated Posting API call is formally unaddressed. It documents adaptive throttling, 429 with `Retry-After`, and `X-RateLimit-Remaining`. |
| `jobs.jobvite.com` | 257 | **Not public.** The developer portal is behind a login; the quotas that circulate come from integration vendors. | Nothing citable. This tool scrapes the server-rendered career site, not an API, so no published policy covers it. |
| `api.eu.lever.co`, `api.lever.co` | 40, 25 | **None** for reading postings. The one documented number is 2 POSTs a second for application submissions, which is a different endpoint. | [Postings API README](https://github.com/lever/postings-api/blob/master/README.md). The 10/s with bursts to 20 in [Lever's developer docs](https://hire.lever.co/developer/documentation) is explicitly "by API key", and this tool sends no key. |
| iCIMS, Workday, Personio, Recruitee, Breezy, Teamtailor, BambooHR | 1 board per host | Irrelevant to pacing, because each employer is its own host and is asked once. Where a limit is documented (iCIMS 10,000 calls a day per customer, Recruitee 1,000 a minute per token, Teamtailor 50 per 10 seconds) it is per customer or per token and nowhere near what one board costs. | iCIMS and Recruitee and Teamtailor developer docs. |

Two consequences worth stating plainly.

**Only one host on this list publishes a number that applies.** SmartRecruiters
says 10 requests a second, and this tool asks it for 3. That is the single
case where the documentation says the pacing is too conservative rather than
leaving the question open.

**For Greenhouse and Ashby there is no published safe rate and no promised
`Retry-After`.** Nothing obliges either host to tell us it is unhappy in a way
the code can read, which is an argument for a conservative default rather than
against one, and it is the same argument this file already makes about
Cornerstone and that `CLAUDE.md` makes about the circuit breaker.

### Where the 3.0 came from

`DEFAULT_PER_HOST_RPS = 3.0` entered in 76d4e98, the commit that stopped a
rate-limit refusal costing eight hours of sleeping. That commit measured a
great deal and states its measurements: 181.3s to 4.4s on twelve real Workable
sources, 97.1 minutes against 49.9 for the interleaving, 1.3x for the pooled
sessions, 82.8s to 46.3s median over eight runs on a 419-source sample. The
one number in it with no measurement attached is this one. It appears as
"3 requests/second by default and 0.7 for Workable, which is the rate the
maintainer's enumerator has sustained overnight without a 429": the evidence
cited is for the 0.7, and the 3.0 is stated rather than derived.

So it is a default nobody measured, and the only reason it has never mattered
is that Workable's 0.7 hid it. Remove Workable and 3.0 becomes the whole
answer to how long a scan takes.

What can be said for it: no host has been recorded refusing at 3.0, and the
sample runs behind that commit drew zero 429s. Every full scan since has put
4,078 requests through `boards-api.greenhouse.io` at that rate, roughly 23
minutes end to end, without a refusal that reached the report. That is
genuine evidence, and it is evidence about the floor rather than the ceiling:
it says 3.0 is safe and says nothing at all about 4, or 6, or 10.

One more thing it is worth being clear about, because it changes what "3.0 is
conservative" means. `CLAUDE.md` tells contributors to be polite at "about one
request a second per host". The code has run at three times that since
per-host pacing was introduced, and nobody updated the sentence.

### A host can be slowed by its own refusals and nothing says so

This is the Workable failure one level up, and it is worth knowing about
before anyone raises a rate on the strength of "we have never seen a 429".

`fetch_one` calls `HostLimiter.note_throttle` on every 429, including the ones
that then succeed on retry, and that widens the host's gap by
`HOST_SLOWDOWN_STEP` up to `MAX_HOST_SLOWDOWN`. That part works and it is the
right behaviour. What is missing is anyone saying it happened:

* A 429 that succeeds on the retry produces an ordinary `Result` with
  `status` 200 and `throttled` False. Nothing distinguishes it from a request
  that was never refused.
* `detect_throttling` only catches a source that returned nothing and has
  returned something before, so a board that came back full after one refusal
  is invisible to it.
* The scan's "is rate-limiting this connection" line only fires for results
  carrying a block, which needs `CONSECUTIVE_429_LIMIT` refusals in a row.
* `HostLimiter.note_throttle` returns the multiplier now in force, and its
  own docstring says that is "so a caller can say out loud that it has slowed
  down rather than doing it silently". No caller reads it. `slowdown_for` has
  no caller either.

So a host refusing one request in four at 3.0 today would look exactly like a
host that is fine, while quietly running at half or a quarter of the rate the
table asks for, and the run would be slower than the floor predicts with
nothing on screen to say why. The fix belongs in `fetch.py` and is small:
`fetch_all` should return, or hand to `on_result`, the hosts whose slowdown
ended above 1.0, and the scan should name them the way it already names a
blocked host. Until that exists, "no 429s were reported" is not the same
statement as "no 429s happened", and any rate raised on the first should be
raised knowing it is not the second.

### Concurrency is a separate dial and it becomes the binding one

`DEFAULT_CONCURRENCY` is 16 and `MAX_CONCURRENCY` is 64. While a paced host
sets the floor, raising the first does nothing at all: the extra workers park
in `HostLimiter.wait`. Simulating the fetch phase over the bundled list, with
Workable removed and every other rate left at 3.0, gives the same 22.7 minutes
at 16, 24, 32, 48 and 64 workers. That is the shape to expect whenever pacing
binds.

It stops being the shape as soon as the rates come up. The other floor is
total request-seconds divided by workers, which after Workable is about 259
request-minutes, so 16 workers cannot beat roughly 16 minutes however polite
or impolite the pacing is. Raising Greenhouse and Ashby past about 4 requests
a second therefore buys nothing at all until the worker count goes up with it.

### What each host actually tolerated, measured 2026-08-27

Measured after a full scan had finished, so the hosts had just taken a normal
day's traffic rather than being rested. Every probe used the scan's own User-
Agent and Accept headers, real board tokens off the bundled list so nothing
was answered from a per-URL cache, a pool with one shared per-host slot clock
so the rate asked for was the rate achieved, and a hard stop on the first 429
or 403. None of them stopped.

A first attempt is worth recording because it measured the probe rather than
the host: single threaded, it asked for 6 requests a second and achieved 2.25,
because a host answering in 0.45s cannot be asked six times a second by one
thread. Any rate probe that does not report the rate it actually reached is
reporting its own latency.

| Host | Sustained | Requests | Result |
|---|---|---|---|
| `boards-api.greenhouse.io` | **6.39/s for 56s** | 360 | 360 x HTTP 200. No 429, no 403. Latency p50 0.137s. |
| `api.ashbyhq.com` | **6.36/s for 47s** | 300 | 300 x HTTP 200, 5,193 postings. Empty-board share 0/150 in the first half and 1/150 in the second, so it was not silently answering with empty arrays either. |
| `api.smartrecruiters.com` | **7.75/s for 39s** | 300 | 300 x HTTP 200. No 429. Documented allowance is 10/s. |

Ashby needed the extra check because a status code cannot answer this question
there: it returns HTTP 200 with an empty array both for a token that does not
exist and for one being rate-limited, so a probe watching for 429 on that host
watches the wrong thing and would report "no refusals" while being refused.

Not probed, deliberately. `jobs.jobvite.com` is 257 boards and 1.4 minutes,
it is a scraped career site rather than an API, and it sits behind Cloudflare;
there is no case for pushing it when the whole prize is 84 seconds. The two
Lever hosts are 65 boards between them, 13 seconds, and equally not worth a
request. `apply.workable.com` is measured elsewhere and is not touched here.

Neither Greenhouse nor Ashby returns a rate-limit header of any kind. One
request to each showed `boards-api.greenhouse.io` behind CloudFront with
`cache-control: max-age=0, private, must-revalidate`, so every board fetch
reaches their origin, and `api.ashbyhq.com` behind Cloudflare with
`cache-control: public, max-age=60` and `cf-cache-status: HIT`, so some of
that load is absorbed at the edge. That is a real difference in what the two
hosts are being asked to do, and it is the reason Greenhouse gets the more
cautious of two otherwise identical measurements.

### The rates, and the evidence behind each

| Host | Boards | Documented | Tolerated | Today | Recommended | Why |
|---|---|---|---|---|---|---|
| `boards-api.greenhouse.io` | 4,078 | none published | 6.39/s x 360, clean | 3.0 | **5.0** | Nothing is documented, so this rests on measurement alone and keeps about 20% under the highest rate observed clean. Every request is an origin hit. Total volume does not change: 4,078 requests either way, just spread over 13.6 minutes instead of 22.7. |
| `api.ashbyhq.com` | 2,607 | none published | 6.36/s x 300, clean, no empty-200 drift | 3.0 | **5.0** | Same evidence and same margin. Part of the load is served from Cloudflare's cache, which argues for at least as much headroom as Greenhouse rather than less. |
| `api.smartrecruiters.com` | 910 | **10/s**, 8 concurrent | 7.75/s x 300, clean | 3.0 | **8.0** | The only host here with a published number. 8.0 sits under it with margin, and under the documented 8-concurrent rule. Worth 3.2 minutes. |
| `jobs.jobvite.com` | 257 | not public | not probed | 3.0 | **3.0** | 1.4 minutes. Scraped HTML behind Cloudflare, no published policy, nothing to gain. |
| `api.eu.lever.co` | 40 | none for reads | not probed | 3.0 | **3.0** | 13 seconds. |
| `api.lever.co` | 25 | none for reads | not probed | 3.0 | **3.0** | 8 seconds. |
| `*.icims.com` | 1,744 | 10,000/day per customer | n/a | 3.0 | **3.0**, and it is never read | One board per host. The rate is not consulted on a host asked once. |
| `*.myworkdayjobs.com` | 1,489 | none published | n/a | 3.0 | **3.0** | One board per host, but 5.36 requests per board, so this is the one platform where the default rate is consulted on a single-board host: it paces the pages of one tenant's own board. Leave it. Community reporting is that Workday's edge limits by source IP across all tenants at once, which is an argument for keeping the gap rather than removing it. |
| `*.jobs.personio.de` and every other single-board host | ~7,749 hosts | mostly none | n/a | 3.0 | **3.0**, unread | One request each. |

`PER_HOST_RPS` and `DEFAULT_PER_HOST_RPS` both live in `jobradar/fetch.py`.
Adopting the above means three entries in `PER_HOST_RPS` and no change to the
default, which stays 3.0 for the long tail where it is almost never consulted
anyway.

### The projected floor

Measured per-platform on 2026-08-27, `tools/bench_fetch.py --platforms`, with
`apply.workable.com` excluded. Requests rather than boards, because Workday
costs 5.36 requests a board, Phenom 8.12, SuccessFactors 6.38 and Avature 4.38:

    23,713 requests, 190.6 minutes of request-seconds

Two floors, and the scan cannot beat the larger:

    per-host   Greenhouse   4,078 requests / 3.0 per second  =  22.7 min
                            4,078 requests / 5.0 per second  =  13.6 min
               Ashby        2,607 / 3.0 = 14.5 min,  / 5.0 = 8.7 min
               SmartRecr.     910 / 3.0 =  5.1 min,  / 8.0 = 1.9 min
    machine    190.6 request-minutes / 16 workers            =  11.9 min

So the fetch phase, simulated over the whole list with the measured rates:

| Setting | Workers | Fetch phase |
|---|---|---|
| today, Workable included | 16 | 49.8 min |
| Workable gone, everything still 3.0 | 16 | 22.7 min |
| **Greenhouse 5.0, Ashby 5.0, SmartRecruiters 8.0** | **16** | **14.1 min** |
| the same, 24 workers | 24 | 13.6 min |
| Greenhouse and Ashby at 6.0 instead | 24 | 11.4 min |

**Concurrency should stay at 16.** While Greenhouse is paced at 5.0 its own
floor is 13.6 minutes and the machine's is 11.9, so the pacing still binds and
extra workers only park in `HostLimiter.wait`: 24 workers buys 30 seconds and
32 buys nothing at all. The number to remember is that at 16 workers Greenhouse
stops being the constraint above **5.70 requests a second**, and only past
that point is raising `fetch.concurrency` worth anything.

