# job-radar

[![tests](https://github.com/maccydee/job-radar/actions/workflows/test.yml/badge.svg)](https://github.com/maccydee/job-radar/actions/workflows/test.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Watch employers' job boards directly, and only be told about roles that pass
your own filters.

Most job tools are built to make you apply to more things. This one is built to
show you fewer: it reads postings straight from company applicant tracking
systems, normalises twenty-seven different board APIs into one shape, and
drops anything that fails rules you write down once.

```bash
git clone https://github.com/maccydee/job-radar
cd job-radar && python3 install.py
```

That is the whole install. It checks your Python is new enough, creates a
virtual environment beside the checkout, installs the two dependencies, and
hands straight over to setup, which asks for your CV, asks what you are
looking for, and runs the first scan. `install.py` imports nothing outside the
standard library, because it runs before anything is installed.

Prefer to do it yourself, or already have an environment:

```bash
pip install -e .
job-radar setup
```

## Requirements

**Python 3.10 or newer.** `install.py` checks this before it does anything else.

Scanning, filtering and the dashboard need nothing beyond that. The two
dependencies are `requests` and `PyYAML`, and the bundled boards need no API
key, no account and no browser.

**The `claude` CLI is a separate prerequisite**, and only for the features that
write or judge something: the **screen**, **tailored CV** and **cover letter**
buttons in the dashboard, and the `job-radar rank` and `job-radar generate`
commands. Those shell out to headless `claude -p` (see `jobradar/runner.py`),
so they need [Claude Code](https://claude.com/claude-code) installed and signed
in. Install it separately if you want them. Without it, everything else works
in full, and those features report that the CLI cannot be found rather than
failing quietly.

Setup runs the first scan itself rather than leaving you holding a config file
and no evidence any of it works. It is also the run most likely to show up a
mistake worth fixing now, like titles that match nothing or a salary floor that
hides everything, while you are still sitting in front of it.

**Leave that first scan an hour, and there is a floor under that no setting
moves.** The bundled list is 17,807 employer boards and each host is paced on
its own clock (more on that in "Being a good citizen"). `apply.workable.com`
alone holds 2,094 of those boards at 0.7 requests a second, which is fifty
minutes on that one host when it is answering, with everything else
interleaved behind it. Above the floor the runtime follows how many sources
you keep and how many requests you allow in flight, so a shorter list or a
higher `fetch.concurrency` (default 16, capped at 64) moves that part and not
the Workable part. `job-radar scan --limit 200` takes a couple of minutes if
you only want to watch it work.

Afterwards:

```bash
job-radar serve       # the dashboard, at http://127.0.0.1:8765
job-radar list        # the same thing as text
job-radar list --new  # only what arrived since the last scan
job-radar rank        # score the whole board against your CV, cheaply
job-radar scan        # read the boards again
```

The dashboard is optional. Everything it does has a command behind it.

Setup asks for your current CV first and will not finish without one. It is
not optional: every document this writes is built from your real record, and
a missing CV does not degrade the output, it invents it. The path is checked
again each time the config loads, so a CV you moved fails loudly instead of
quietly producing fiction.

Or fork this repo, write a `config.yaml` and commit it with `git add -f`, and
let GitHub Actions run it for you. No server, no Docker, free on public repos.
The full steps are in "Running it on GitHub Actions" below; the `-f` is not
optional, because `config.yaml` is gitignored.

---

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

---

## What it does

**Reads 17,807 employer boards** across 21 board platforms. Roughly 4,100 are
Greenhouse and 2,600 Ashby, then Workable, iCIMS, Workday, Personio, Breezy,
Recruitee, SmartRecruiters and Oracle in the four figures or high hundreds,
then Jobvite in the hundreds, then Avature, Phenom, Teamtailor, Lever on both
its US and EU deployments, Pinpoint and SuccessFactors in the tens. Three more
entries are keyword searches rather than boards, which is what takes the file
to 17,810: LinkedIn's public endpoint, NHS Jobs, and Workable's own
cross-employer search at `jobs.workable.com`, which reaches every employer
Workable hosts rather than the 2,094 boards a crawl happened to find, and
carries the full advert with it.

The code carries 27 adapters, four more than the bundled list uses: Reed and
Adzuna are keyed aggregators you add yourself, JazzHR is written but has no
boards on the list yet, and there is a generic RSS reader for a feed you add
by hand.

`job-radar coverage` counts the file rather than trusting this paragraph, and
that is the number to go by. **Expect it to print slightly more than 17,807.**
The three keyword sources are templates rather than boards. Each is expanded
into one search per job title in your config, up to twelve, and the Workable
search is expanded again per country in `locations.countries` and
`locations.relocate_to`, so the total it prints moves with your config.

Names you would recognise are on it: Barclays, Lloyds, Santander, BP, Shell,
Unilever, Tesco, Marks & Spencer, John Lewis, Sky, Skyscanner, Accenture,
Linklaters, Transport for London, Ofcom and the FCA among them.

Oracle Taleo and BambooHR shipped ahead of their boards and have since picked
some up: three Taleo (Cincinnati Financial, D.R. Horton, Textron) and one
BambooHR (IP Group). JazzHR is still a `discover --add` away rather than
already covered: the adapter can read it, the bundled list carries none of it
yet. Reed and Adzuna are keyed aggregators you switch on yourself.

The list is not hand-written, and at this size it is not `discover` output
either. Most of it came out of a public crawl index, verified board by board,
in a maintainer repository that is not this one. `discover` is what adds the
employer the harvest missed, and a weekly job revalidates the whole list.

**Finds new boards for you.** Point `discover` at a company and it follows the
careers-page redirect chain, extracts the ATS token, and proves it by counting
live postings.

```
$ job-radar discover primer.io
  ashby              39 jobs  [verified]  https://api.ashbyhq.com/posting-api/job-board/primer.io
                   board names itself 'primer'
```

That token is `primer.io`, with the dot. It is not guessable, and this is the
normal case rather than the exception.

**Checks the board is really that company.** A token that responds is not
proof. Ashby `primer` is a Florida micro-schools operator advertising for
teachers, not the London payments company. Greenhouse `peak` is a Texas
physiotherapy chain. Every discovered board is checked against the domain you
asked for, and a mismatch is reported rather than filed.

**Tells you what is new.** State is diffed between runs, so a scan reports the
roles that appeared since last time rather than the same list again.

**Says when it has been throttled.** A board that used to return jobs and now
returns none is reported as suspect rather than empty, because several of these
APIs answer an empty array when they are rate-limiting you.

**Keeps what it could not read.** A board `discover` locates but cannot fetch
is reported as `[could not read]` rather than folded into "nothing found",
which is three false statements at once about a board that was actually
found. It counts as a result, but `--add` will not write it into your config:
an unread board is a guess, and banking a guess into the source list is worse
than leaving it out.

---

## The salary rule

This is the one piece of behaviour worth understanding before you trust the
output.

- A posting with a **stated** salary below your floor is **hidden**. You
  already know it is too low.
- A posting with **no stated salary** is **shown**, marked *unconfirmed
  salary*.

Most employers still publish no figure. On a sample Greenhouse board, 21 of 37
roles carried a pay range and 16 did not; filtering out the unstated ones would
have thrown away nearly half the board. So only a number the employer actually
published can disqualify a role.

Day and hourly rates are annualised before the comparison, because £600 a day
is £132,000 a year and not £600.

Salaries in a different currency to your floor are never silently converted.
They are kept and flagged, since a wrong exchange-rate assumption drops real
jobs quietly.

---

## A dashboard you can work from

```bash
job-radar serve
```

Opens the same dashboard at `127.0.0.1:8765`, but every row has buttons and
what you click sticks. It is a local server, standard library only, and it
stops when you Ctrl-C. Not a daemon, not something to expose.

**Screen** reads the job description against your dealbreakers and gives a
verdict in seconds for pennies. Do this before anything expensive: a role with
a coding round is cheaper to find now than after you have drafted a CV for it.

**CV** drafts one tailored to the posting. **Cover letter** is disabled until
the CV exists, because the letter is checked against it: no sequence of six or
more words may appear in both. The CV carries the facts, the letter carries
judgement, and they should share nothing but your name.

**Apply** opens the posting and marks it. **Skip** strikes it through and it
stops coming back.

Generation runs headless `claude -p` in the background using the `rate-cv`,
`natural-writing` and `screen-role` skills, then runs objective gates: slop
score, em-dash count, phrase overlap, rating, and any figure or scale word in
the draft that is not in your CV. The results are recorded and shown against
the document; nothing is redrafted for you, because a second pass costs tokens
you did not ask for. A document with a failed gate is one to read before you
send, not one to trust. It never claims a document is finished, only drafted.

**Nothing generates unless you click it.** No schedule, no watcher, no
speculative drafting. Every token spent is one you asked for.

Documents land in `~/job-applications/<date>-<company>-<role>/`, outside the
repository, alongside a snapshot of the job description. That snapshot matters:
postings are pulled the moment they are filled, which is usually just before
someone calls you about one.

Requires the `claude` CLI on your PATH, and the
[natural-writing](https://github.com/maccydee/natural-writing) skill installed
at `~/.claude/skills/natural-writing/`, because the drafting prompts run its
linter and the slop-score gate reads its output. Without it you still get
documents, but two of the four gates quietly stop reporting. Everything other than
generation works with neither.

---

## Everything works without a browser

The dashboard is a convenience, not the product. Every action has a command:

```bash
job-radar list                      # roles, statuses, documents, ratings
job-radar list --status applied     # or --all, or --json
job-radar applied <url|company|uid> -s interviewing --note "call booked"
job-radar generate <url|company|uid> -k screen      # or cv, or cover_letter
```

A scan filters what it fetched that day and never looks back, so a change to
your titles, locations, dealbreakers or salary floor only applies to roles
found afterwards. `job-radar rescreen` re-applies the current config to what
is already stored and reports what no longer matches. It removes nothing
unless you add `--remove`, and even then a role you have already acted on
stays: the status is a decision you made and it outranks a filter.

```bash
job-radar rescreen             # report only
job-radar rescreen --remove    # delete the untouched ones that no longer match
```

`applied` and `generate` accept a posting URL, a company name, or a role id.
When a name matches more than one role they list the candidates and stop
rather than guessing, because recording a status against the wrong role is
worse than not recording it.

Both write the same database the dashboard reads, so the two views cannot
disagree.

`scan`, `enrich`, `rescreen`, `discover`, `validate` and `coverage` are
command-line only by design: they are slow maintenance verbs and the dashboard
has nowhere sensible to show their progress.

---

## Remembering what you already did

A scanner that forgets is a scanner that shows you the same job every week.
This one keeps a record of what happened next, and the roles you have settled
stop coming back.

```bash
job-radar applied https://job-boards.greenhouse.io/example/jobs/123456
job-radar applied "Example Corp" --status rejected --note "coding round"
job-radar applied <url> --status interviewing --note "second round"
```

That writes to the local database, the same one the dashboard reads, so the
two cannot disagree. The database lives at `data/job-radar.db` by default and
is gitignored, so your history stays yours even on a public fork.

`applications.local.yaml` is still read if you have one, and is imported once
on first run into the default database. A scan pointed elsewhere with `--db`
imports neither it nor `state/seen.json`, because `--db` reads as isolation
and has to be one. It is no longer written to; see
[`applications.example.yaml`](applications.example.yaml) for the format if you
are migrating an old file.

Statuses run `new → interested → applied → submitted → interviewing → offer`,
plus `rejected`, `withdrawn`, `skipped` and `closed`. `skipped` is what the
dashboard's **Skip** button writes.

**The four settled ones are hidden from results** rather than shown again.
Everything else gets a status pill on the dashboard, so a role you are three
rounds into is obvious at a glance instead of sitting anonymously in a list of
three hundred.

Matching is by URL when you give one. Otherwise it uses the company name and a
loose title match, so an entry you typed by hand still finds the posting when
the wording differs. Giving only `org` mutes a whole company.

---

## Configuration

Everything lives in `config.yaml`, which the repo does not ship: it is
gitignored, so a fresh clone has none and nothing you write there ever
conflicts on a pull. `job-radar setup` writes one by asking questions; after
that, edit it directly. Setup needs a terminal to ask them, so for a script
there is `job-radar setup --defaults --cv PATH --titles "a,b"`, and
`config.example.yaml` is a starting point to copy.

```yaml
titles:
  include: [engineering manager, head of engineering]
  exclude: [product manager, account manager]

locations:
  countries: [UK]
  remote_ok: true
  relocate_to: [US, CA]
  need_sponsorship: [US, CA]
  exclude: [Paris, Dublin]

salary:
  floor: 90000
  currency: GBP

dealbreakers:
  - name: coding round
    pattern: "take.?home|live coding|coding (?:test|assessment|challenge)"
    hard: true

sectors: []          # empty means all
```

Every setting, what it accepts and what happens when it is wrong is in
[docs/CONFIG.md](docs/CONFIG.md), including the command-line flags and the
real list of sectors.

`titles.include` is matched twice. The whole-word regex runs first, and
anything it misses gets a second pass from a looser matcher that accepts the
same words in another order with up to two words between them, so
`engineering manager` also finds "Manager, Engineering Platform" and
`head of engineering` finds "Head of Site Reliability Engineering". A word
that changes the job rather than rewording it, "product", "business" or
"program" landing inside the phrase, still blocks the match: "Engineering
Program Manager" does not pass.

`dealbreakers` are read against the job description, which is the part that
catches roles that look right in a search result and are wrong in the detail.

Put private settings in `config.local.yaml`, which is also gitignored and
takes precedence. Both are ignored, so a fork using the Actions path has to
force-add `config.yaml` to commit it.

---

## Running it on GitHub Actions

1. Fork this repo and clone your fork.
2. Write a `config.yaml`: `job-radar setup`, or copy `config.example.yaml`
   and edit it. The repo ships neither, because `config.yaml` is gitignored.
3. Commit it: `git add -f config.yaml && git commit && git push`. The `-f`
   is required, because the file is gitignored and a plain `git add` refuses
   it in silence. Without it the runner checks out a fork with no config and
   the scan stops on "No config at config.yaml". You do not need to add
   `state/`; the workflow force-adds that itself on every run.
4. Uncomment the two `schedule:` lines at the top of
   `.github/workflows/scan.yml`.

That last step is not optional and it is not an oversight. The cron is
commented out **in this repository** on purpose, because this repo is public
and a scheduled scan would publish its maintainer's job search to anyone with
the URL. Your fork is where the decision belongs, so it is left to you to make
rather than inherited by default. Without it the workflow still runs, but only
when you press the button (`workflow_dispatch`).

Once it is scheduled, the scan runs at 07:00 UTC on weekdays, commits its
state, posts new roles as an issue, and attaches the dashboard as a workflow
artifact you can download.

### How you get the dashboard, and who else can see it

There are three ways, and the difference between them is who is allowed to
look:

| | Who can see it | Cost |
|---|---|---|
| **Workflow artifact** (default) | anyone who can see the repo | free, public or private repo |
| **Issue per run** | anyone who can see the repo | free, public or private repo |
| **GitHub Pages** (opt in) | **anyone on the internet with the URL** | free on public repos only |

**Pages is off by default and you should think before turning it on.** A Pages
site has no login. Making your fork private does not fix that: private-repo
Pages needs a paid plan, and even then the published site is still world
readable unless you are on Enterprise with access control. Publishing means
putting the roles you are looking at, and the filters that produced them, on
the open web under your own username.

If that is fine by you:

```bash
gh variable set PUBLISH_PAGES --body true
```

then Settings → Pages → Source: GitHub Actions. Each fork serves its own site
at `https://<you>.github.io/job-radar/`; there is no shared host and nothing
to run.

**If you want it private, use a private fork.** Artifacts and issues both work
there, on the free plan, and neither is visible to anyone who cannot already
see your repo. Private repos get 2,000 free Actions minutes a month, which is
far more than a daily scan uses.

### Two caveats worth knowing

**On a public fork, your search is public** even without Pages. `config.yaml`
has to be force-added for the workflow to read it, and `state/` is committed
on every run, so the titles you search for, your salary floor, your dealbreakers
and every role you have been shown are all readable. A private fork, or
running locally with your settings in the gitignored `config.local.yaml`,
avoids that.

**Actions runners share IP ranges** with a very large number of repositories,
so job boards throttle them sooner than they throttle your laptop. The scan
flags sources that look throttled; if you see many, run it locally instead.

**State is committed, not cached.** The Actions cache is evicted after seven
days of no use, which would make every role look new again. `state/` is
gitignored so this repository never carries its maintainer's search history;
the workflow force-adds it, so a fork gets working state without the upstream
repo collecting one.

---

## Notes on the APIs

These are the things that cost a debugging session each. They are the reason
the adapters are shaped the way they are.

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

## Keeping the source list current

The list is data, and data rots. Boards migrate between applicant tracking
systems, tokens get renamed, companies get acquired. One revalidation pass
found 23 dead boards, and 19 of those had simply moved ATS and were hiding
762 live roles that the scan could no longer see.

A weekly job in this repository revalidates every board on Sunday mornings and
opens a pull request pruning anything dead. Growing the list is a separate job
that does not live here: the crawl-index harvest that found most of these
17,807 boards runs in a private maintainer repository, so that forking this
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

## What a job posting can and cannot do to you

Descriptions come from thousands of third-party servers, and anyone can post a
job to a job board. That text ends up in two places that matter: a prompt, and the
working directory of a subprocess that can write files. So it is treated as
hostile input rather than as content.

- **It is fenced and labelled.** The description sits between explicit markers
  in `job-description.md`, the markers are stripped out of the text first so a
  posting cannot close the fence, and every prompt says that anything inside
  is a claim about a job and never an instruction.
- **The subprocess cannot reach your skills.** It used to be handed
  `--add-dir ~/.claude/skills` alongside `--permission-mode acceptEdits`,
  which meant write access to every skill you have. The skills a job needs are
  copied into that job's own folder instead, so a compromise can only damage a
  directory that exists for one role.
- **Bash is narrowed to one script**, the writing linter, rather than any
  Python at all.
- **Ranking numbers roles by position**, not by an id the model could be
  talked into forging, and each position may be answered once. A posting that
  tries to score a different role gets ignored and named in the output.
- **Links are scheme-checked.** The apply URL comes from third-party JSON and
  on several platforms is employer-supplied, so `javascript:` and `data:` are
  dropped rather than rendered.

What that does not do is make an agent immune to persuasion. Prompt injection
has no complete fix, and a determined posting may still get a model to say
something odd in a draft you are going to read anyway. The point of the
measures above is that the blast radius is one job folder and one document,
not your skills, your other roles, or your machine.

**The dashboard binds to 127.0.0.1 and checks that.** It validates the Host
header against the address it actually bound to rather than against the
Origin, because both headers are attacker-controlled together under DNS
rebinding and used to agree with each other. `/open` will only reveal files
this tool recorded making.

## What this does not cover

Worth saying plainly, because a tool that quietly fails at something looks
broken rather than out of scope.

**Roles not posted to an ATS with a public API.** This design covers most
white-collar hiring. Trades, care work and retail floor jobs largely do not
work this way and are better served elsewhere.

**Fields you cannot select for, because most of the list is still unlabelled.**
This used to read as a shortage of employers. It is now a shortage of tags. Of
17,822 sources, **6,140 carry a sector tag and 11,682 carry none**: the harvest
that took this list from hundreds to thousands read board addresses out of a
public crawl index, and an address does not say what industry the employer is
in. The tagged ones are 1,310 healthcare, 1,303 finance, 512 education, 498
media, 409 energy, 407 retail, 405 technology, 311 construction, 238
transport, 222 telecoms, 161 public sector, 90 security, 74 hospitality, 65
charity, 43 legal, 42 industry, 34 professional services and 16 travel.

That is less damaging than it sounds, because **a `sectors:` filter keeps every
untagged source as well as the ones you asked for**. `sectors: [hospitality]`
does not cut you to seventy-four employers; it drops the sources tagged as
something else and leaves the 11,682 unlabelled ones in, which is where most
of any industry actually is. The cost runs the other way: you cannot ask this
list for "every hospitality employer" and get a true answer, and `job-radar
coverage` can only report what somebody labelled.

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
how much of the tagged list applied to them. 90 are tagged `security` now, up
from an initial 34 that was mostly a relabelling of names already in the
list — CrowdStrike, Darktrace, SentinelOne, Snyk, Semgrep, Wiz, Okta, Rapid7,
Proofpoint, Sophos, Qualys, Tenable and Zscaler among them — and almost
entirely product vendors: thin at the MSSP and consultancy end (S-RM was the
only one) and short on anything outside the UK and US.

Three passes closed part of that gap, each doing a different job. The first
went looking for boards not yet in the list at all and found eight: Coalfire,
Optiv, Arctic Wolf, Praetorian and Cyderes on the consultancy side, Trend
Micro (Japan), Group-IB (Singapore) and Cybereason (Israel/US) outside the UK
and US. The second went the other way, scanning the 17,810 company names
already in the list for ones that read as security and had been filed under
`technology`, `finance`, `telecoms` or nothing — 45 of them, Keeper Security,
Securityscorecard, Obsidian Security, Armis Security, Cato Networks, Eye
Security (Netherlands), Cybervadis and Obrela Security Industries (a
Greek/UK MSSP) among them, mislabelled rather than missing. On2It was filed
under `healthcare` and Eye Security under `finance`; no amount of adding new
employers would ever have surfaced those. The third went
back to `discover` with a much longer, deliberately MSSP/consultancy-shaped
list of names — Trustwave, NCC Group, Kroll, BlueVoyant, eSentire, Deepwatch,
CyberCX, Redscan and around forty others tried across two sittings — and
found six more live boards among them: Tevora, BreachLock, Horizon3.ai and
ReliaQuest (US), ON2IT (Netherlands, zero-trust MSSP) and usd AG (Germany).
That list ran well under half hits; most of the well-known MSSP and
consultancy names guessed at do not resolve on the platforms this reads, and
are reported as such rather than silently dropped.

Every one of the 57 retagged or newly added entries was checked two ways: the
board URL still answers with a real posting (a plain GET for most platforms,
a POST with a body for Workday, which is what ReliaQuest needed), and the
company identity was confirmed against an outside source rather than trusted
on the strength of matching a regex. That second check is what caught the
regex's false positives — Siemens (the string `siem` sitting inside a longer
word), Cyberpuerta (an electronics retailer), Security Finance (a consumer
lender) and half a dozen physical-guarding and alarm firms whose names happen
to end in "Security".

The harder exclusions are the ones where the company really does do security
and the *board* still does not qualify. Nuspire's board now belongs to PDI
Technologies, the parent that acquired it, and lists PDI's whole hiring
pipeline. CyberMedia Technologies is a federal IT modernisation contractor
whose board advertises acquisition specialists and budget analysts alongside
its cyber work. Both stay under their existing tags, for the same reason the
Big Four (Deloitte, PwC, KPMG, EY) and Accenture do despite each running a
large cyber practice: the board has to be the practice, not merely contain
it. A tag that catches every employer with a security team is a tag that
means nothing to the person filtering on it.

Still light on continental European MSSPs specifically outside the handful
above, and a run through Bitdefender, ESET, Orange Cyberdefense, NVISO,
Wavestone, Devoteam, CyberProof and the like turned up no live board on the
platforms this reads, which usually means a platform this does not support
(Taleo, SuccessFactors, a bespoke career site) rather than no board at all.

**Whole sectors on platforms not yet supported.** An early harvest of UK
employers resolved 34 boards from 196 attempted, and healthcare resolved
**zero of 25**, because NHS trusts use NHS Jobs rather than any commercial
applicant tracking system. That one is fixed: there is an NHS Jobs adapter,
which also picks up the private providers who advertise there. SuccessFactors
and Oracle have adapters now too. What is left unreadable is TRAC, Civil
Service Jobs, Eploy, Hireserve, Jobtrain, Networx, Oleeo, Oracle EBS
iRecruitment and CharityJob, and an adapter for any of them would do more for
coverage than another hundred technology companies. `discover` names all of
those except TRAC when an employer turns out to be on one.

Civil Service Jobs deserves its own note, because it is the largest single
employer board in the UK and there is no useful coverage of UK public policy
roles without it. It is not merely unwritten: the search page sits behind a
bot interstitial and answers a plain request with "Quick check needed", so an
adapter is real work rather than an afternoon. `discover` names it when an
individual department points at it.

**Employers who block automated requests.** Some large consumer brands put
their careers site behind bot protection. Tesco's careers site answers 403
from Akamai to a discovery request, Sainsbury's replies "You got banned
permanently from this server", and `jobs.louisvuitton.com` answers 403.
`discover` reports those as blocked and stops. Nothing here attempts to defeat
bot protection, and it never will.

A blocked front door is not always a blocked employer, which is worth knowing
before writing one off. Tesco's board itself is an ordinary Avature board at
`careers.tesco.com` and reads fine once it is in the list by hand, which is
where both Tesco and Tesco Bank came from. Louis Vuitton's group boards were
already on SmartRecruiters under LVMH. What the block stops is finding them
automatically, not reading them.

**Salary you can filter on reliably.** Around a third of postings state one.
That is the market, not a bug, and the rule above is built around it.

**Currencies other than your own.** A salary in a different currency to your
floor is never converted. It is shown, marked "not compared", and can neither
disqualify a role nor earn it points. That is deliberate: a wrong exchange
rate silently drops real roles. The consequence, if your floor is in euros, is
that a sterling role below it stays on the list with a note rather than
disappearing.

**The right to work.** A posting that states its sponsorship position is
flagged "no sponsorship" or "sponsorship offered", read from the description.
Most postings state nothing, and nothing here is filtered on it. If you need
sponsorship, treat an unflagged role as unknown rather than as available.

**Several UK applicant tracking systems.** Charity and public-sector
recruitment runs largely on Eploy, Hireserve, Jobtrain, Networx, Oleeo and
Oracle EBS iRecruitment, and none have adapters. `discover` recognises all of
them and tells you which one an employer uses, with the working board URL,
rather than saying "nothing found". It still cannot read them. A fundraiser
trying twenty-one organisations resolved one. Adapter contributions for these
would do more for coverage than another hundred technology companies.

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

---

## robots.txt, and what this tool does about it

**It does not check robots.txt, and some of what it fetches is disallowed by
it.** That is a deliberate choice and you should know about it before you run
this. Every place it happens is listed below rather than left for you to find.

**Bot protection is a different thing, and it is never worked around.** The
distinction matters and it is not a shade of grey. A `robots.txt` is a file
that asks, addressed to crawlers following links, and ignoring it is a policy
choice this repository owns in public. A CAPTCHA, a Cloudflare or Akamai
interstitial, or a token an application mints at runtime for its own
JavaScript is a control that refuses, and getting past one is breaking an
authentication check rather than declining a request. Nothing here does the
second, and where a site refuses it is recorded as refused and left alone:
Cornerstone OnDemand's `401 no Authorization header found`, Civil Service
Jobs' "Quick check needed", Tesco's careers site answering 403 from Akamai,
Sainsbury's "You got banned permanently from this server", and
`jobs.louisvuitton.com`'s 403. `discover` reports each of those as blocked and
stops there.

Most sources here are public JSON APIs that applicant tracking systems publish
for exactly this purpose, and those are not contentious. The exceptions are
the sources parsed from HTML. Checked against each host's robots.txt for a
generic agent:

| Source | robots.txt |
|---|---|
| NHS Jobs, Serco and Thales (Phenom), Transport for London (SuccessFactors), Metro Bank (Avature), OSB Group (iCIMS) | allowed |
| **Oracle Taleo** (TTEC, BAE Systems, Transport for London) | no rule to break: `<tenant>.taleo.net/robots.txt` is a **404** on every tenant checked, so nothing is disallowed. Checked 2026-08-24. |
| **LinkedIn** (`jobs-guest` endpoint) | **`Disallow: /`** |
| **Reed** (`/api/1.0/search`) | allowed. `User-agent: *` has no `/api/` rule; only `PerplexityBot` is disallowed from it. |
| **Adzuna** (`api.adzuna.com/v1/api/jobs/...`) | **`Disallow: /`**. The API host's `robots.txt` is two lines long and blanket-disallows every agent. See below. |

So the bundled LinkedIn source fetches a path LinkedIn's robots.txt tells
crawlers not to. It is one entry that gets expanded into one search per job
title you configure, up to twelve, so the request count follows your
`titles.include` rather than being fixed. They are public pages served without a login, and this reads
them at a handful of requests per run rather than at crawl scale, but that
does not make it permitted.

`enrich` then fetches the full posting for each LinkedIn result it kept, one
request per role, on the same endpoint family and under the same caveat. That
is the larger share of the traffic, not the searches.

**What that means for you.** LinkedIn may block the IP you run this from, and
they take a harder line on scraping than most. If that matters to you, delete
the single entry with `"platform": "linkedin"` from `sources/sources.json`, or
set `sources.use_bundled: false` and list your own. `scan --no-enrich` turns
off the per-role fetch on its own. Everything else in the tool works without
any of it.

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

If you are running this at work, on shared infrastructure, or anywhere the
consequences are not purely yours, read that table before you press go.

## Being a good citizen

Concurrency (how many different boards are read at once) defaults to 16 and is
capped at 64, but that number governs breadth, not how hard any one host is
hit: each host is paced on its own clock, roughly 3 requests a second, slower
for the strict ones (Workable's limit is 0.7, learned from throwing 250 live
employers away in one run by outrunning it). Requests to different hosts are
interleaved rather than sent in the order the source list happens to store
them, so 4,100 consecutive Greenhouse entries do not park the whole pool on
one host while everything else waits. A host that answers three different
sources with 429 in a row, having already used up their retries, is treated
as saying no rather than asking for a pause: it is blocked outright for five
minutes rather than retried into. A `Retry-After` under a minute is honoured
as a pause; over a minute it is read as a refusal for the rest of the run.
Retries otherwise use backoff, and the user agent identifies the tool and
links here.

These are other people's servers, and a job board that starts blocking
scrapers makes the market worse for the people using this.

The bundled source list is **data**, shipped in the repo. Building it is a
maintainer operation that runs elsewhere; forking this does not set a crawler
loose.

---

## Skills

`skills/` holds Claude Code skills that pair with the scanner. `rate-cv` also
ships as [its own repository](https://github.com/maccydee/rate-cv), which is
the source of truth; the copy here is synced weekly by a workflow. See
[skills/README.md](skills/README.md).

---

## Development

```bash
python3 -m pytest -q            # 395 passed
python3 tests/run_all.py        # the same suite, without pytest: 395/395
job-radar validate --file sources/sources.json --report out/validation.json
job-radar coverage              # what the source list actually holds
```

`tests/run_all.py` discovers every `tests/test_*.py` and needs nothing
installed, pytest included. It is what CI runs, and naming one file instead is
the mistake it exists to prevent: CI ran `tests/test_core.py` and nothing
else, so `tests/test_locations.py`, which holds every country-code rule that
decides whether a job is one you can legally take, had never executed once.
Seven files hold the 395 tests between them, and a new `test_*.py` runs here
and in CI without anyone editing a workflow.

The suite has under-reported itself two other ways, both fixed and both worth
knowing before you add to it. The `__main__` block that collects `globals()`
once sat partway up `test_core.py`, so it saw only the tests defined above it
and ran less than half the file while printing what looked like a full pass;
it has to stay at the end now, and the file's own comment says why. And the
runner caught `Exception` rather than `BaseException`, so one test raising
`SystemExit` ended the whole run mid-file with no failure line and no summary.

MIT licensed.
