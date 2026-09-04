# UK contract job boards: what job-radar can actually read

Evidence gathered 2026-09-04, one honest identified GET per check (`User-Agent:
job-radar/0.1 (research; contact mcdonaldcallum@hotmail.co.uk)`), paced at
roughly one request a second per host, no user-agent rotation, no proxies, no
headless browser, stopped after two failures on any host. Every board below
was tested live; none of this is carried over from `docs/SOURCES.md` without
being re-checked, because two of the findings below directly contradict what
that file currently says and the dates matter.

**Headline: most of the named contract boards are unreadable, exactly as
`docs/SOURCES.md` predicts for general job boards. But three are not, and one
of them - ContractorUK - is a better fit for this repo than anything else in
`docs/SOURCES.md` bar Reed. `docs/SOURCES.md`'s existing note that
"Totaljobs / CWJobs ... hangs until the 30 second timeout" is stale: both
answered cleanly today. The two new LinkedIn sources are the other headline: they are live, but their filters do nothing.**

## Summary table

| Board | Readable | Mechanism | Key needed | Evidence |
|---|---|---|---|---|
| JobServe | **No** | none found | - | robots.txt 200 disallows the search page itself (`Job-Search.aspx`); the live search page (`/gb/en/Job-Search/`) is 200 text/html, 114KB, but is a jQuery/ASP.NET WebForms page with zero job links in the raw HTML and no RSS `<link>`. The one JS file that talks to a backend (`searchJobServe.js`) only calls autosuggest/history endpoints (`/WebServices/JobSearch.asmx/getHelp`, `SearchHistory.asmx`), not a results API. |
| CWJobs | **Yes (scrape)** | server-rendered HTML, no JSON API | No | `GET /jobs/contract/engineering-manager` → 200 text/html, 1.08MB. See sketch below. |
| Technojobs | **No** | - | - | `www.technojobs.co.uk` fails DNS resolution (curl exit 6). Bare `technojobs.co.uk` resolves to a Cloudflare IP but both `https://` and `http://` connections time out after 15s (curl exit 28) - two failures, stopped per the rate limit rule. Functionally dead. |
| Totaljobs | **Yes (scrape)** | server-rendered HTML, no JSON API | No | `GET /jobs/contract/engineering-manager` → 200 text/html, 1.08MB, 85 matching postings. See sketch below. **This reverses `docs/SOURCES.md`'s 2026-08-24 finding that the connection "hangs until the 30 second timeout." It answered in 1.2s today.** |
| CV-Library | **No - blocked** | - | - | robots.txt (200) is now more permissive than `docs/SOURCES.md` records (`Allow: *?jobId=` and `*?page_number=` are carved out of an otherwise blanket `Disallow: /*?`), but a plain GET to a search page (`/engineering-manager-jobs`) answers **HTTP 403** from Cloudflare, page body contains "challenge". Blocked, stop. |
| Jobsite | **No - not worth it** | server-rendered HTML exists, but no usable posting URL | No | `GET /jobs/contract/engineering-manager` → 200 text/html, 1.08MB, 73 results, same StepStone card markup as CWJobs/Totaljobs - but the only link in each card goes to `/tp-out`, a robots-disallowed apply-redirect, not a canonical posting URL. Same underlying inventory as Totaljobs with a worse link shape. |
| IT Job Board (theitjobboard.co.uk) | **No** | - | - | Both `www.theitjobboard.co.uk` and `theitjobboard.co.uk` fail DNS resolution outright (curl exit 6, twice). Domain is dead. |
| ContractorUK | **Yes (scrape)** | server-rendered HTML, no JSON API | No | `GET /all_contract_jobs?q=engineering+manager` → 200 text/html, 152KB, 122 pages of real, keyword-matched results. See sketch below. Best fit of everything checked. |
| JobServe RSS | **No** | - | - | No RSS `<link rel="alternate">` on the search page, robots.txt names no feed path, no feed URL found. See JobServe row - the same request budget covers both. |
| Free-Work UK | **No** | client-rendered, not a plain GET | - | `GET /en-gb/tech-it/jobs?query=engineering+manager` → 200 text/html, 320KB, Nuxt SSR. The embedded `__NUXT_DATA__` payload carries only page chrome (nav, forum threads, blog posts) and the **sitewide unfiltered** `jobPostingCount: 8413` - zero job postings and zero `/job/` links anywhere in the raw HTML. The query-matched list is fetched by client-side JS after load; reading it needs a browser, which this exercise does not run. |
| LinkedIn contract (`f_JT=C`) | **Live but broken** | guest HTML endpoint (already in repo) | No | 200, 10 postings returned for "engineering manager" - **identical set, identical order, identical URLs** to the unfiltered baseline query. The filter does nothing. See dedicated section. |
| LinkedIn temporary (`f_JT=T`) | **Live but broken** | same | No | Same result: 200, 10 postings, **identical to baseline**. |

## Adapter sketches for the three readable boards

All three sit on plain server-rendered HTML with no JSON API underneath - an
adapter here is a scrape, using the same selector-based approach the repo
would otherwise reserve for a platform with no other option. None of them
needed a key, a login, or anything past a plain GET.

### ContractorUK - the strongest candidate

- **URL shape.** Keyword search is `https://www.contractoruk.com/all_contract_jobs?q={keyword}`
  (the more obvious `/contract_jobs?keywords=` path served the *unfiltered*
  default list - a trap worth documenting in the adapter itself, since a
  reader who doesn't check would get a page of teaching-assistant and PDI
  technician postings and believe the query had matched them).
- **Paging.** `&page=1`, `&page=2`, … A "Last page" link in the pager gives
  the true count directly - no need to infer it from a result total. For
  "engineering manager" alone, last page was **122**, page size **20**
  (confirmed by the URL param on the pager, not guessed). robots.txt has a
  dated comment that pagination is meant to be readable: *"Pagination
  (?page=) stays crawlable"* (3 Sep 2026) - the only one of the four
  StepStone/ContractorUK-family robots.txt files that opens paging
  unconditionally.
- **Fields on the card** (`article.cuk-job-card`, confirmed against real
  HTML): `.cuk-job-card__title a[href]` (title + canonical URL, e.g.
  `/job/437259-engineering_manager`), `.cuk-job-card__loc` (location),
  `.cuk-job-card__summary` (a real description snippet, several sentences,
  not Adzuna's truncated 500 characters), `.cuk-job-card__posted` (relative
  text: "6 days ago" - needs parsing to a date, same problem the repo already
  solves elsewhere), `.cuk-badge--sector` (sector tag). Day rate and IR35
  status are **not** a separate field - they live inside the free-text
  summary ("£48-£53 per hour (Outside IR35)") and need the same salary-text
  parser the repo already runs over Reed/PCSX bodies.
- **Explicit CONTRACT/PERMANENT field: none needed.** ContractorUK is a
  contract-only board by construction - every posting on it is a contract,
  confirmed by a zero-hit search for "Permanent" across a full results page.
  That is the one thing that makes it categorically different from every
  general board on this list: no facet, no filter, no misread field, because
  there is nothing to misread.
- **What robots.txt actually says**, worth being exact about the way
  `docs/SOURCES.md` is for Reed and Adzuna: `Disallow: /job/*/apply` (the
  apply link, which the same file says redirects to a CV-Library affiliate
  URL - so applying through this source re-exposes CV-Library indirectly),
  `/contract-jobs/autocomplete`, and the ad-click paths under `/media/promo/`
  and `/ClickTrack/`. Job listing and job detail pages are not on that list.
- **Apply link is a CV-Library affiliate redirect.** Worth flagging for the
  same reason the repo already flags Reed's employer-vs-agency problem: the
  company name on the card is the real employer, but the apply flow itself
  goes through a third party.

### Totaljobs (StepStone) - build here, not on CWJobs

- **URL shape.** `https://www.totaljobs.com/jobs/contract/{keyword}` - the
  `/contract/` path segment is a genuine facet, confirmed by content (day
  rates, "Outside IR35" postings) rather than by trusting the URL's name.
  `https://www.totaljobs.com/jobs/{keyword}` with no `/contract/` segment
  returns the unfiltered board (677 results for "engineering manager" against
  85 once the contract facet is applied).
- **Paging.** `?page=N`. robots.txt explicitly allows this for Totaljobs in a
  way it does **not** for CWJobs: `Allow: /jobs/*?page=2$` through `$5$`
  (and the same for `?q=*&page=2..5`), so pages 1-5 (125 results) are
  robots-permitted; page 6 onward is not. CWJobs' robots.txt has the
  identical allow-lines present but **commented out** (`#Allow: ...`), so
  CWJobs disallows everything past page 1. That is the concrete reason to
  build against Totaljobs rather than CWJobs: **the two sites are the same
  backend and the same job pool** (CWJobs' own card links resolve to
  `totaljobs.com/job/...` URLs, not to a `cwjobs.co.uk` URL - confirmed on a
  live card), so CWJobs adds nothing except a more restrictive robots.txt.
- **Fields on the card** (`article[data-at="job-item"]`): `[data-at="job-item-title"]`
  (title), `[data-at="job-item-company-name"]`, `[data-at="job-item-location"]`,
  `[data-at="job-item-salary-info"]` (free text: "£80 - £90 per hour", "Up to
  £72,000 per annum"), `[data-at="job-item-timeago"]` (relative date), and the
  canonical URL from the first `<a href>` inside the card matching `/job/`.
  `data-resultlist-offers-total` on the results container gives the true
  count directly, the same trick as ContractorUK's "Last page" link.
- **Explicit CONTRACT/PERMANENT field: not on the card.** It comes from
  which URL you hit, not from a field in the payload - an adapter has to
  request the `/jobs/contract/...` facet specifically and cannot recover the
  distinction from a general `/jobs/...` result by inspecting the row.

### CWJobs - do not build separately

Same markup, same backend, same job pool as Totaljobs, confirmed by CWJobs'
own job cards linking out to `totaljobs.com`. Its robots.txt is stricter
(page 1 only, versus Totaljobs' pages 1-5), and it offers nothing Totaljobs
doesn't already have. Listed here only so nobody re-discovers it and builds
a second adapter for the same postings.

## The two new LinkedIn sources

Tested against the repo's own fetch path (`jobradar.fetch.fetch_one` +
`jobradar.adapters.platforms.parse_linkedin`), one call per source, keyword
"engineering manager", `location=United Kingdom`, `f_TPR=r604800` (last 7
days) held constant so only the job-type parameter differed between calls.

| Source | Status | Postings returned | Sample titles |
|---|---|---|---|
| LinkedIn (baseline, already shipped) | 200 | 10 | Technical Engineering Manager - Backbase (Cardiff); Engineering Manager - Raylo (London Area); Engineering Team Lead (ETL) - Backbase (Cardiff) |
| LinkedIn contract (`f_JT=C`) | 200 | 10 | **Identical** three: Technical Engineering Manager - Backbase; Engineering Manager - Raylo; Engineering Team Lead (ETL) - Backbase |
| LinkedIn temporary (`f_JT=T`) | 200 | 10 | **Identical** three: same as above |

Comparing the full 10-item result sets (not just the first three): the URLs,
titles, companies and order are **byte-for-byte the same set** across all
three queries. `f_JT=C` and `f_JT=T` are silently ignored by the public guest
endpoint - the raw HTML response differs slightly (30,779 vs 30,767 vs 30,791
bytes, almost certainly tracking parameters and impression IDs on the same
cards), but the postings themselves do not change.

**Verdict: not worth keeping as filters, and actively worth removing before
anyone relies on them.** They are not the "returns nothing" failure this
repo's whole `CLAUDE.md` is about - they are worse, because they return
real, correctly-shaped jobs and look exactly like a working contract filter.
A scan run today would count "LinkedIn contract: 10 postings" as a success
and nobody would notice those 10 are duplicates of the unfiltered LinkedIn
source already being read, tagged as contract-only when none of them were
verified as such. The base "LinkedIn" source stays exactly as useful as it
was; these two add cost (three requests instead of one, per keyword) for
zero new information. Most likely mechanism: LinkedIn's public/guest jobs
surface only honours a small parameter allow-list (keywords, location,
distance, a handful of others); `f_JT` is a filter the logged-in jobs search
UI supports and the guest endpoint appears to accept syntactically without
acting on it. Not independently confirmed against the authenticated surface,
since that would need a login this exercise does not do - stated as the
likely explanation, not a verified one.

## Ranked recommendation

1. **ContractorUK.** Best payoff-to-effort ratio of anything examined here,
   Reed included: every posting is already a contract by construction (no
   type-detection risk), the description snippet is real prose rather than a
   500-character truncation, day rate and IR35 status are present in text on
   the same page you already fetched, and robots.txt explicitly welcomes
   pagination. Effort is a straight HTML-scrape adapter (`.cuk-job-card`
   selectors are stable and simple), a relative-date parser, and reuse of
   the salary-text parser this repo already has for Reed/PCSX. The one thing
   to get right and test explicitly, the way `CLAUDE.md` asks: the apply
   link is a CV-Library affiliate redirect, not the employer's own
   application form, and the working search endpoint (`/all_contract_jobs?q=`)
   is not the more obvious-looking `/contract_jobs?keywords=` path, which
   silently serves the unfiltered board - exactly the kind of thing this repo
   has shipped as a silent bug before.

2. **Totaljobs, contract facet only.** A general board, so most of its
   volume is permanent and has to be filtered out at the URL rather than
   trusted from a field - but the `/jobs/contract/{keyword}` facet is real,
   verified by content, and the site answers cleanly today despite
   `docs/SOURCES.md` recording a timeout as of 2026-08-24. Worth a second
   look specifically because that entry is now wrong, not because Totaljobs
   is a natural fit for this repo's model: unlike Reed/Adzuna/ContractorUK it
   has no documented API and no key, it is a scrape of the same shape as
   ContractorUK, and its own robots.txt caps polite pagination at 125
   results per keyword (pages 1-5). Build this second, and skip CWJobs
   entirely - it is the same backend with a stricter robots.txt and zero
   independent postings.

Everything else on the list is a genuine no, not a "didn't get to it": two
domains are dead (Technojobs, IT Job Board), one is blocked outright by
Cloudflare on the first honest request (CV-Library, HTTP 403), one has no
JSON backing anything a plain GET can reach (JobServe, no RSS either), one is
client-rendered so a plain GET returns page chrome and not results
(Free-Work UK), and one duplicates a board already covered with a worse link
shape (Jobsite). If the honest summary has to be one sentence: **most of the
named contract boards really are unreadable, exactly as `docs/SOURCES.md`
already argues for general job boards - but ContractorUK and Totaljobs are
not, `docs/SOURCES.md`'s Totaljobs/CWJobs note is stale and should be
corrected, and the two new LinkedIn sources are live but their filters are
inert and should not be trusted as contract-specific.**
