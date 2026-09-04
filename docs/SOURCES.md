# Where the postings come from

[job-radar](../README.md)

The bundled list is 17,811 employer job boards, read straight from the
applicant tracking system each employer runs, plus three keyword searches.
This is the long version: why employer boards rather than an aggregator, the
two aggregators that are in and how to switch them on, every well-known job
board that was checked and rejected, and what the source list actually holds.

## Why go to the boards directly

Aggregators are broad but noisy: reposts, agency duplicates, roles that were
filled a month ago. Company ATS APIs are the source those aggregators scrape,
so postings appear there first and appear once.

The trade is coverage. This only sees employers on the list, which is why
`discover` exists.

### The one aggregator worth the trouble: Reed

That coverage gap is real and it does not close by adding employers one at a
time. A mid-size British employer running a careers page nobody has harvested
is invisible to every source above, and a lot of them advertise on Reed.

Reed is in for one reason the other aggregators are not: it publishes a
[documented REST API](https://www.reed.co.uk/developers/jobseeker) and hands
out a free key for it. No scraping, no browser, no working around a bot check.
It is off by default and it does nothing until you switch it on.

**Getting a key.** Fill in the three-field form at
<https://www.reed.co.uk/developers/jobseeker> (first name, last name, email)
and it is emailed to you. Free, no card, no paid tier. Then either:

```yaml
sources:
  reed_api_key: ""       # put it here in config.local.yaml, which is gitignored
  extra:
    - company: Reed
      url: "https://www.reed.co.uk/api/1.0/search?keywords={keyword}&postedByDirectEmployer=true"
      platform: reed
      country: UK
      keyword_template: true
```

or leave `reed_api_key` blank and export `REED_API_KEY` instead, which is what
GitHub Actions wants. **Never put a real key in `config.yaml`.** Both files
are gitignored, but `config.yaml` is the one a fork force-adds for the Actions
path, so it is the one that ends up committed. With no key the Reed source is
skipped and the scan says so by name.

`{keyword}` expands into one search per entry in `titles.include`, the same
way NHS Jobs works, so that single line becomes up to twelve searches
(`sources.MAX_KEYWORD_TITLES`). A scan names any title past the cap rather
than dropping it silently.

**What to expect from it.** These are aggregator listings and they are not the
equal of an employer's own board:

- **The apply link goes through reed.co.uk**, not the employer's form. Every
  Reed role is flagged so you can see which kind of link you are following.
- **`employerName` is whoever posted the job.** On an agency listing that is
  the agency, so the company name on the row may not be the company you would
  be working for. `postedByDirectEmployer=true` in the URL above asks Reed for
  employers only, which is the shipped default. Take it out if you want the
  agency listings too.
- **Duplicates.** The same vacancy is listed once per agency that has it.
  Filtering to direct employers cuts most of that at the query; the rest is
  handled by the existing dedupe, which collapses roles on company and title
  and prefers the more direct source. A role that reaches you from both Reed
  and the employer's own Greenhouse board should show up once.
- **Salary needs care.** The search endpoint gives numbers with no period
  attached, so a bare `650` might be a year or a day. Anything under 2,000 is
  treated as an unlabelled rate and left unconfirmed rather than annualised on
  a guess, which means it is shown to you and cannot be used to disqualify the
  role. Reed also lets an employer hide the salary, in which case nothing
  comes back at all, which is the same as any other board.
- **Location is a bare town, so the country gets added.** Reed's
  `locationName` is free text ("Stoke-on-Trent", "Cambridgeshire") and the
  location matcher cannot place most of Britain's towns and counties on its
  own, so the adapter appends ", United Kingdom" itself rather than lose the
  role to a country filter. It only does that where the string does not
  already name a country outright, so "Dublin, Ireland" is left alone. That
  test is deliberately narrow: it looks for a country name, not for whether
  the town itself could belong to somewhere else, so a bare "Perth" or
  "Boston" is read as not naming a country and gets the UK suffix, which is
  right for the Scottish Perth and the Lincolnshire Boston that make up
  almost everything Reed carries, and wrong on the rare listing that really
  is Perth, Australia or Boston, Massachusetts typed the same bare way.

### The other one: Adzuna, and the countries you would move to

Adzuna is in for the same reason Reed is, plus one Reed cannot offer. It
publishes a [documented REST API](https://developer.adzuna.com/overview) with a
free self-serve key, and it runs **nineteen national indexes**. The country is
two letters in the URL path, so watching the places you would relocate to is a
copy of one line with `gb` swapped for `us`, `ca` or `au`.

The nineteen are `gb us at au be br ca ch de es fr in it mx nl nz pl sg za`.
There is no United Arab Emirates index, so Adzuna does nothing for a Dubai
search.

**Getting the credentials.** Register at
<https://developer.adzuna.com/signup>; the dashboard then shows an `app_id` and
an `app_key`. Free, no card. Both go in the config:

```yaml
sources:
  adzuna_app_id: ""      # config.local.yaml, which is gitignored
  adzuna_app_key: ""
  extra:
    - company: Adzuna
      url: "https://api.adzuna.com/v1/api/jobs/gb/search/1?title_only={keyword}&results_per_page=50"
      platform: adzuna
      country: UK
      keyword_template: true
```

or export `ADZUNA_APP_ID` and `ADZUNA_APP_KEY`, which is what GitHub Actions
wants. With no credentials the source is skipped and the scan says so by name.

**Watch the call budget.** Adzuna's published free limits are **25 hits a
minute, 250 a day, 1,000 a week and 2,500 a month**. One scan is one call per
job title per page, and a keyword source is searched with up to twelve titles:
twelve at up to three pages is **36 calls**, so the monthly cap is the binding
one and it works out at roughly **two scans a day** sustained, or about six in
a single day against the daily cap. Fewer titles cost proportionally less. Add
a second country and double it.

**What to expect from it.**

- **The salary may be a guess.** Adzuna attaches a figure to most adverts, but
  `salary_is_predicted` is `"1"` when it came from their own Jobsworth model
  rather than from the employer. Those are never treated as confirmed, so they
  can never disqualify a role, and the row says the number is an estimate. A
  figure the advertiser actually stated is used normally.
- **No direct-employer filter.** Reed has `postedByDirectEmployer`; Adzuna has
  nothing equivalent, so agency listings arrive mixed in and
  `company.display_name` is whoever placed the advert.
- **Descriptions are truncated to 500 characters** by Adzuna's own
  documentation, so what you get is a preview, not the advert. `enrich` cannot
  expand it either: `redirect_url` is a redirector, not a posting page.
- **Duplicates.** Adzuna aggregates from employer career sites and other
  boards, so a good share of what it returns is a role this tool can already
  read from the employer's own applicant tracking system. Those collapse into
  the employer's row rather than showing twice: Adzuna is scored as an
  aggregator by `screen.directness`, so the direct board always wins the row
  and the Adzuna copy becomes an "also listed on adzuna" note.
- **Contract roles.** Adzuna annualises a day rate before publishing it, which
  is how a six month contract clears a permanent salary floor. Anything marked
  `contract_type: contract` is flagged as contract rather than permanent.
- **Location is a town and a county, so the index's country gets added.**
  `display_name` never names a country, so the adapter appends the one the
  URL's index is watching, `gb` becomes ", United Kingdom", the same way the
  Reed adapter does it and for the same reason: only where the string does
  not already name a country. The same narrow gap applies, a bare "Perth" on
  the British index reads as unnamed and is filed as British rather than
  Australian.
- **Credentials travel in the query string.** Adzuna offers no header
  authentication, so there is no alternative. They are added per request and
  are never written onto the stored source, into the state file, or into an
  error message.

**Their terms, and what they ask of you.** Adzuna's API terms list the
permitted uses as "Publishing Adzuna ad listings", "Publishing Jobsworth salary
estimates" and "Personal research", and for that third one the obligation is
attribution: an API user "shall acknowledge Adzuna as the source of all salary
and vacancies data wherever it is published". Running it over your own job
search publishes nothing. If you do publish anything you pulled through it,
credit Adzuna. Anything beyond personal use, by "a commercial, government or
academic organisation", is limited by their terms to a 14 day trial without a
licence agreement.

### Indeed is not in, and will not be

Indeed retired its public Publisher API and did not replace it with anything
self-serve. What is left is partner-gated and employer-side, granted "entirely
in Indeed's sole discretion" after an approval process, and its terms say you
"shall not embed the Indeed API in third party systems". There is no keyed
route in for a tool like this one.

The public site is not a route either. A plain, honest, identified GET to
`uk.indeed.com/jobs` answers **403** with "Security Check ... Additional
Verification Required. Please enable JavaScript to complete the security
check." The RSS path is gone (404, and disallowed in robots.txt besides).
Getting past that check means rotating user agents, driving a headless
browser, or paying a proxy service to launder the requests, which is a
different activity from reading a published feed and is not something this
repo is going to do. Indeed is actively refusing automated access, and the
answer to that is to take no for an answer.

### The rest of the aggregators, and why they are not here

Indeed is not a special case. Every well-known job board was checked the same
way, on 2026-08-24, with one honest identified GET each. Two got in. Here is
what happened to the others, so nobody has to check them again.

| Source | What happened |
|---|---|
| **Find a job** (DWP, `findajob.dwp.gov.uk`) | **Gone.** Every path answers **HTTP 503** with a GOV.UK page reading "This site is now closed". There is nothing left to read. |
| **Civil Service Jobs** | Blocked by a human check. The front page is HTTP 200 and its content is "Quick check needed. We just need to confirm you're a real person. Please check the box below and then click Continue." Getting past that is the thing this repo does not do. Note the status code: 200 proves nothing. |
| **Totaljobs / CWJobs** (StepStone) | No public API. A plain GET to a search page does not answer at all: the connection is accepted and then hangs until the 30 second timeout. |
| **CV-Library** | `robots.txt` for `User-agent: *` has `Disallow: /api/` and `Disallow: /*?`, and their search is a query string. The partner feed needs a commercial agreement. |
| **Guardian Jobs**, **Monster**, **Workinstartups** | **403 on `robots.txt` itself.** Monster's is a DataDome interstitial ("Please enable JS and disable any ad blocker"); the other two are a CloudFront "Request blocked". A site that will not serve you its own robots.txt has answered the question. |
| **Technojobs** | The domain does not resolve. |
| **Wellfound** (AngelList Talent) | No public API, and `robots.txt` has `Disallow: /search`. |
| **Otta / Welcome to the Jungle** | `robots.txt` has `Disallow: /*?`, which is every search. The API behind the site is partner-gated. |
| **Escape the City** | `robots.txt` allows everything, but the search runs on Algolia with keys embedded in the page. Lifting keys out of a page is not using a published API. |
| **Talent.com** | `robots.txt` names their own search API and disallows it: `Disallow: /services/api-new/search`. |
| **Dice** | `Disallow: /jobs?q*`, `/rss/` and `/feed/`. US-centric anyway. |
| **Jooble** | The API needs a key granted on request, and the page documenting it is behind a Cloudflare "Just a moment..." challenge. |
| **Careerjet** | A real documented API, and it is **HTTPS-refused**: port 443 is closed and it answers only over plaintext HTTP. It also requires a `Referer` naming the page calling it, which a command line tool does not have. A credential over cleartext plus a header we would have to invent is not a route in. |
| **ZipRecruiter**, **Glassdoor** | Partner APIs behind an approval process. No self-serve tier. |
| **Hacker News "Who is hiring"** (via the free Algolia HN API) | Reachable and genuinely open, and not worth an adapter. The August 2026 thread held 242 comments; 19 mentioned a leadership title anywhere in the text, 20 mentioned the UK or London, and **2 did both**. The format is prose with a "company | role | location" convention that is a habit rather than a schema, and pay is rarely stated. |
| **Arbeitnow** | Free, no key, `robots.txt` allows it, and it carries a lot of UK. It is still out, for a reason worth knowing: **it has no search**. `?search=engineering%20manager` returns byte-identical results to no parameter at all, so an invalid query looks exactly like a successful one. Reading four pages of it, 550 postings covering two days, found 116 UK locations, 3 engineering-leadership titles, and **0 that were both**. Its `remote` boolean is also true for roles whose location is a London office. |
| **Himalayas** | 104,915 jobs and no way to narrow them. `country=`, `search=` and `seniority=` are all accepted and all ignored, returning the same unfiltered newest-first page, and `limit=100` returns 20. Reading the whole thing is 5,000 requests, which is a crawl, not a handful. |
| **We Work Remotely** | Free RSS, allowed by `robots.txt`, and too thin to justify a parser: the all-jobs feed held 88 items with **one** engineering-leadership title, and 87 of the 88 give their region as "Anywhere in the World" while the advert underneath says things like "open to candidates located in British Columbia or Ontario". The one structured field it has is wrong on almost every row. |
| **RemoteOK** | The whole API is the latest 100 postings, there is no search, the first element of the array is a legal notice rather than a job, and its terms require a followed backlink from a website in exchange for access. There is no website here to put one on. |
| **Remotive** | `robots.txt` for `User-agent: *` contains `Disallow: /api/*`, which is the documented public API. |
| **Working Nomads** | Free and open. 42 items, none of them leadership. |

The pattern in the remote-first boards is worth saying plainly: they are real,
they are open, and for a senior engineering leader they are close to empty.
Between Arbeitnow, Himalayas, We Work Remotely, RemoteOK and Working Nomads,
one live snapshot held **five** engineering-leadership titles across roughly
800 postings, and none of them was UK-eligible.

## What the source list holds

**Fields you cannot select for, because most of the list is still unlabelled.**
This used to read as a shortage of employers. It is now a shortage of tags. Of
17,815 sources, **6,093 carry a sector tag and 11,722 carry none**: the harvest
that took this list from hundreds to thousands read board addresses out of a
public crawl index, and an address does not say what industry the employer is
in. The tagged ones are 1,311 healthcare, 1,304 finance, 512 education, 498
media, 409 energy, 407 retail, 405 technology, 311 construction, 239
transport, 224 telecoms, 161 public sector, 74 hospitality, 65 charity, 43
legal, 42 industry, 34 security, 34 professional services and 16 travel.

That is less damaging than it sounds, because **a `sectors:` filter keeps every
untagged source as well as the ones you asked for**. `sectors: [hospitality]`
does not cut you to seventy-four employers; it drops the sources tagged as
something else and leaves the 11,722 unlabelled ones in, which is where most
of any industry actually is. The cost runs the other way: you cannot ask this
list for "every hospitality employer" and get a true answer, and `job-radar
coverage` can only report what somebody labelled.

**The country tags work the same way, and `sources.countries` obeys the same
rule.** 5,215 sources carry no country tag and 1,597 are tagged `multi`, which
means the board belongs to a multinational rather than to one country. Both
kinds are fetched whatever you set, because "we could not tell" and "several
countries" are not evidence that the employer has nothing where you are, and
a multinational is one of the likelier places to find a vacancy in yours. So
`sources.countries: [UK]` leaves 7,748 sources rather than the 936 tagged
`UK`: it skips the boards that can be proved to be somewhere else, and
`locations.countries` is what actually decides where a role is.

So even in a sector that looks well covered above, the real number is larger
still, and in one that looks thin the gap is in the tagging, not necessarily
in what this can read. Where it genuinely is thin, adding the employer beats
any setting in the config: `job-radar discover <employer> --add` takes about a
minute. Nando's is on Workday and Hilton is on Oracle, both of which this
reads. UK public policy is the honest weak spot regardless: 35 of the boards
carrying the `public-sector` tag are British, Transport for London, the FCA,
the Information Commissioner's Office, the Care Quality Commission, UKRI and a
handful of councils among them. The tag runs to 161 in total; most of the rest
is the same name-based rule catching US municipal and non-profit employers
rather than more UK coverage. Civil Service Jobs, which is where most UK
public-policy hiring actually happens, cannot be read at all.

**`security` is its own sector tag.** Vendors used to be filed under
`technology`, so `job-radar coverage` had no way to tell a security engineer
how much of the tagged list applied to them. 34 are tagged `security` now:
CrowdStrike, Darktrace, SentinelOne, Snyk, Semgrep, Wiz, Okta, Rapid7,
Proofpoint, Sophos, Qualys, Tenable and Zscaler among them. That was mostly a
relabelling, not new employers. It is almost entirely product vendors, and
thin at the MSSP and consultancy end: S-RM is the only one so far. `discover`
finds these the same way as anywhere else; nobody has pointed it at that end
of the market yet.

Oracle Taleo and BambooHR shipped ahead of their boards and have since picked
some up: three Taleo (Cincinnati Financial, D.R. Horton, Textron) and one
BambooHR (IP Group). JazzHR is still a `discover --add` away rather than
already covered: the adapter can read it, the bundled list carries none of it
yet. Reed and Adzuna are keyed aggregators you switch on yourself.

The list is not hand-written, and at this size it is not `discover` output
either. Most of it came out of a public crawl index, verified board by board,
in a maintainer repository that is not this one. `discover` is what adds the
employer the harvest missed, and a weekly job revalidates the whole list.

## Keeping the source list current

The list is data, and data rots. Boards migrate between applicant tracking
systems, tokens get renamed, companies get acquired. One revalidation pass
found 23 dead boards, and 19 of those had simply moved ATS and were hiding
762 live roles that the scan could no longer see.

A weekly job in this repository revalidates every board on Sunday mornings and
opens a pull request pruning anything dead. Growing the list is a separate job
that does not live here: the crawl-index harvest that found most of these
17,811 boards runs in a private maintainer repository, so that forking this
does not set a crawler loose. **Neither of them reaches your copy.**

- **Cloned it?** Your source list is frozen at the day you cloned.
  `git pull` brings the merged updates down.
- **Forked it?** Your fork runs its own validation, so it prunes dead boards
  for you. It never gains new ones: the crawler that finds employers lives in
  a separate private repository on purpose, so that forking this does not set
  a crawler loose. Pull from upstream for those.

```bash
git pull                                  # a clone
git pull https://github.com/maccydee/job-radar main   # a fork
```

`job-radar scan` says so itself once the list is more than a month old, and
`sources/sources.json` carries the date it was last checked in its `meta`
block if you want to see for yourself.

## Reed and Adzuna, against their robots.txt and their terms

**Reed is not the same case as LinkedIn, despite both being read over an
API.** Reed's `robots.txt` allows `/api/` for `User-agent: *`; the only agent
disallowed from it is `PerplexityBot`. Reed also publishes that exact path as
a developer API, documents it, and runs a signup form to hand out the key it
requires, so a key is a permission rather than a workaround.

Their Website Terms and Conditions contain **no anti-robot, anti-spider or
anti-scraping clause at all**. What they restrict is load ("you must not ...
seek to overload the system via spamming or flooding") and republication ("You
may copy material on the Website for your own private or domestic purposes,
but no copying for any commercial or business use is permitted"). A personal
job search, keyed, at a handful of requests per run, is inside both.
Publishing the listings you pull would not be.

**Adzuna is the awkward one, and it is worth being exact about it.**
`https://api.adzuna.com/robots.txt` is, in full:

```
User-agent: *
Disallow: /
```

That is a blanket disallow on the API host, checked on 2026-08-24. It is not
qualified by agent and there is no exception for `/v1/api/`. Set against that:
the same company runs a developer portal that documents this exact endpoint,
hands out the credentials it requires through a signup form, publishes an
OpenAPI description of it, and lists "Personal research" as a permitted use in
its API terms. A `robots.txt` addresses crawlers following links; a client the
operator issued credentials to is a different thing, and the terms are the
document that speaks to it.

You may still not think those reconcile, and the honest position is that they
do not obviously do so. So it is stated here rather than smoothed over, and
Adzuna, like Reed, is **off by default**: it does nothing until you register,
put credentials in your config, and add the source yourself.
