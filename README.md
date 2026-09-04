# job-radar

[![tests](https://github.com/maccydee/job-radar/actions/workflows/test.yml/badge.svg)](https://github.com/maccydee/job-radar/actions/workflows/test.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Watch employers' job boards directly, and only hear about the roles that pass
your own filters.

It reads 17,811 employer job boards straight from the applicant tracking
systems companies run, normalises 23 board platforms into one shape, and drops
anything that fails rules you wrote down once. It is for one person running
their own search on their own machine, and it is built to show you fewer
things rather than more.

Aggregators are the obvious alternative, and they are broad but noisy:
reposts, agency duplicates, roles that were filled a month ago. Employer
boards are the source those aggregators scrape, so a posting appears there
first and appears once. The trade is coverage, which is what `discover` is
for:

```
$ job-radar discover primer.io
Looking for primer.io...
  ashby              32 jobs  [verified]  https://api.ashbyhq.com/posting-api/job-board/primer.io?includeCompensation=true
                   board names itself 'Primer'

Re-run with --add to write these into your config.
```

That is a real run, on the day this paragraph was last checked. The job count
is whatever Primer happened to be advertising that morning and will not be 32
when you try it; everything else on the line is the part worth reading.

`[verified]` is the word that took the work. The token is `primer.io`, with
the dot, and it is not guessable, which is why this reads it off the careers
page rather than trying names. A board that answers is not proof either: Ashby
`primer` is a Florida micro-schools operator advertising for teachers, and
Greenhouse `peak` is a Texas physiotherapy chain, so every board found this
way is checked against the company you asked for before it is filed.

Reading rather than running? [Under the hood](#under-the-hood) is the short
version, and [what breaks on each of 23 board platforms](docs/PLATFORMS.md) is
the long one.

---

## Install

```bash
git clone https://github.com/maccydee/job-radar
cd job-radar && python3 install.py
```

That is the whole install. It checks your Python is 3.10 or newer, creates a
virtual environment beside the checkout, installs the two dependencies
(`requests` and `PyYAML`), and hands straight over to setup, which asks for
your CV, asks what you are looking for, writes the config and runs the first
scan. `install.py` imports nothing outside the standard library, because it
runs before anything is installed.

Already have an environment, or prefer the steps:

```bash
pip install -e .
job-radar setup
```

**There is no `config.yaml` in the repo.** It is gitignored, so a fresh clone
has none, `job-radar setup` writes yours, and nothing you put in it ever
conflicts on a pull. Setup needs a terminal to ask its questions, so for a
script there is `job-radar setup --defaults --cv PATH --titles "a,b"`, and
`config.example.yaml` is a starting point to copy.

Setup asks for your CV first and will not finish without one. Every document
this writes is built from your real record, and a missing CV does not degrade
the output, it invents it. The path is checked again each time the config
loads, so a CV you moved fails loudly instead of quietly producing fiction.

Then:

```bash
job-radar serve       # the dashboard, at http://127.0.0.1:8765
job-radar list        # the same thing as text
job-radar list --new  # only what was first seen on the most recent day
job-radar scan        # read the boards again
```

**Leave that first scan an hour**, and there is a floor under that no setting
moves. Each host is paced on its own clock, and `apply.workable.com` alone
holds 2,094 of the bundled boards at 0.7 requests a second, which is fifty
minutes on that one host when it is answering, with everything else
interleaved behind it. Above the floor the runtime follows how many sources
you keep and how many requests you allow in flight (`fetch.concurrency`,
default 16, capped at 64). `job-radar scan --limit 200` takes a couple of
minutes if you only want to watch it work.

**The `claude` CLI is a separate prerequisite**, and only for the features
that write or judge something: the screen, CV and cover letter buttons in the
dashboard, and the `job-radar rank` and `job-radar generate` commands. Those
shell out to headless `claude -p` (see `jobradar/runner.py`), so they need
[Claude Code](https://claude.com/claude-code) installed and signed in, and
they spend tokens every time you press them. Without it, scanning, filtering
and the dashboard work in full, and those features report that the CLI cannot
be found rather than failing quietly.

---

## Skip the slow hour, if you want to

The first scan takes about an hour, and nearly all of that hour is three slow
platforms. If you would rather have a full dashboard today, there is a file
you can download instead: somebody has already read those boards, and the
result is published for anyone to take.

**What it is.** A snapshot of every role on Ashby, Greenhouse and Workable's
own boards, 8,779 employer boards in all, taken by the same code you are
running and rebuilt every Sunday. The advert text comes with it, so a role
that arrives this way can be screened, ranked and drafted against exactly like
one you fetched yourself.

**Whether to use it.** Use it if you want roles in front of you in a minute
rather than in an hour. Skip it if you are happy to wait, because a scan finds
the same roles and fresher ones.

**What you give up**, and none of it is subtle:

- **It is not a scan and it does not replace one.** Everything that is not
  Ashby, Greenhouse or Workable is missing from the file entirely. That is the
  first five minutes of a scan, so skipping it saves you almost nothing.
- **It is up to a week old.** Roles die in days. A seeded role may already be
  filled, and the file has no way to tell you so.
- **A scan's answer wins on every field.** So run one anyway. The sequence
  that works is: load the seed, start a scan, read the dashboard while the
  scan corrects it underneath you.

**`job-radar setup` fetches it for you**, so a fresh install has roles in it
before the first scan has finished. `setup --no-seed` turns that off. To fetch
it by hand, or into a config you already have:

```bash
job-radar seed load https://github.com/maccydee/job-radar/releases/download/seed-latest
```

It reads an index of what is published, works out which country files your
`locations.countries` needs, and downloads only those. A UK reader takes
roughly 38MB of about 240MB. The command prints the exact figures for your
own countries before it fetches anything, and they change every week, which
is why they are not written down here. Roles whose country could not be read,
and roles open in several countries at once, come with every download,
because neither is evidence that a job is somewhere else.

Those are rows rather than distinct adverts. A job open in six towns is
written once per town, and the database keeps one row per posting, so around
2% of a UK download is the same advert twice. The count is simply larger than
the number of jobs, which is why the roles stored come out fewer than the
roles `seed load` reports as matching.

What arrives is screened against **your** config, exactly as a scan is. The
file carries no score, no fit and no reasons: those are answers to a question
only your own settings ask, and nobody else's filters can reach you. The
request sends a user agent and nothing else.

The download is kept, so a second config or a second machine does not fetch it
again and a dropped connection resumes rather than starting over. Say where it
goes with `--keep DIR`; without that flag the location is worked out from
`--config` and is easy to lose track of. `--dry-run` reports what would be
stored without writing to the database, but it still downloads the file,
because it has to read the roles to screen them.

`docs/SEED.md` has the format, the shard sizes and how to build your own.

---

## What a scan gives you

A scan ends with the roles that passed your filters, and `job-radar list`
prints them again afterwards: one role to two lines, the score, the title, the
company and either the stated salary or `unconfirmed`, with the role's id and
location underneath. `job-radar serve` is that same list with buttons.

**17,811 employer boards across 24 platforms**, plus four cross-employer
feeds: LinkedIn, NHS Jobs and Workable's own search, which are keyword
templates expanded against your titles, and Workable's recently-posted feed,
which takes no keyword. That is what takes `sources/sources.json` to 17,815
entries. Roughly 4,100 of the boards are Greenhouse and 2,600 Ashby, then
Workable, iCIMS, Workday, Personio, Breezy, Recruitee, SmartRecruiters and
Oracle in the four figures or high hundreds, down to Pinpoint and
SuccessFactors in the tens. Names you would recognise are on it: Barclays,
Lloyds, Santander, BP, Shell, Unilever, Tesco, Marks & Spencer, John Lewis,
Sky, Skyscanner, Accenture, Linklaters, Transport for London, Ofcom and the
FCA among them. The code carries 32 adapters, five more than the bundled
list uses.

`job-radar coverage` counts the file rather than trusting that paragraph, and
prints rather more than 17,815, because each of the three keyword templates
becomes one source per title, and per country for the one that takes a
country. Where the list came from, and the two keyed aggregators you can add
yourself, are in [docs/SOURCES.md](docs/SOURCES.md).

**Tells you what is new.** State is diffed between runs, so a scan reports the
roles that appeared since last time rather than the same list again.

**Says when it has been throttled.** A board that used to return jobs and now
returns none is reported as suspect rather than empty, because several of
these APIs answer an empty array when they are rate-limiting you.

**Keeps what it could not read.** A board `discover` locates but cannot fetch
is reported as `[could not read]` rather than folded into "nothing found",
which is three false statements at once about a board that was actually found.
It counts as a result, but `--add` will not write it into your config: an
unread board is a guess, and banking a guess into the source list is worse
than leaving it out.

### The salary rule

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

## The dashboard, and the commands behind it

```bash
job-radar serve
```

Opens the same list in a browser at `127.0.0.1:8765`, except that every row
has buttons and what you click sticks. It is a local server, standard library
only, and it stops when you Ctrl-C. Not a daemon, not something to expose.

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
documents, but two of the four gates quietly stop reporting. Everything other
than generation works with neither.

### Everything works without a browser

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

### Remembering what you already did

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

`config.yaml` is written by `job-radar setup` and edited by hand afterwards.
Put private settings in `config.local.yaml`, which takes precedence. Both are
gitignored, so a fork using the GitHub Actions path has to force-add
`config.yaml` to commit it.

```yaml
titles:
  include: [engineering manager, head of engineering]
  exclude: [product manager, account manager]

locations:
  countries: [UK]
  remote_ok: true
  work_modes: []     # empty means all; [remote] means remote only
  relocate_to: [US, CA]
  need_sponsorship: [US, CA]
  exclude: [Paris, Dublin]

salary:
  floor: 90000
  currency: GBP

sources:
  countries: [UK]    # see below: this narrows the list less than it looks

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

`locations.work_modes` is an allow-list of `remote`, `hybrid` and `office`,
and empty, the default, keeps all three. It is the setting `remote_ok` cannot
express: `remote_ok: false` says you do not want to work from home, which is
not the same as `work_modes: [remote]`, which says you will not take anything
else. A posting that states no arrangement at all is **kept whatever you set**
and marked "not stated", because about half of them state nothing and reading
"we cannot tell" as "not remote" hides more real remote roles than it removes
office ones. `unstated` is refused as a value for the same reason: it is not a
working arrangement anybody chooses.

`sources.countries` narrows the list far less than the name suggests, and
deliberately. It only drops a board whose country tag names somewhere else.
Of the 17,815 sources, 5,215 carry no country tag at all and 1,597 are tagged
`multi`, and both kinds survive whatever you set: "we could not tell" and
"this employer is in several countries" are not evidence that the employer has
nothing where you are, and a multinational is one of the likelier places to
find a vacancy in your country. So `sources.countries: [UK]` leaves 7,748
sources, not the 936 that carry a `UK` tag. It is a way to skip the boards you
can prove are somewhere else, not a way to get a UK-only list, and the
`locations` filters above are what actually decide where a role is. Run
`job-radar coverage` to see the tag counts for the list you are really using.

---

## What it cannot do

A tool that quietly fails at something looks broken rather than out of scope,
so these are stated rather than left for you to find.

- **Roles not posted to an applicant tracking system with a public API.** This
  design covers most white-collar hiring. Trades, care work and retail floor
  jobs largely do not work that way and are better served elsewhere.
- **Employers who are not on the list.** Coverage is the list, and the list
  only grows when you add to it: `job-radar discover <employer> --add` takes
  about a minute. The crawl-index harvest that built it runs in a private
  maintainer repository, so forking this does not set a crawler loose.
- **Platforms with no adapter.** Cornerstone OnDemand is 589 employer hosts
  and a dead end rather than an unwritten adapter, because its API answers
  `401 no Authorization header found` to everything and the page mints its own
  token at runtime. Civil Service Jobs, TRAC, Eploy, Hireserve, Jobtrain,
  Networx, Oleeo, Oracle EBS iRecruitment and CharityJob have no adapters
  either, which is most of UK charity and public-sector hiring. `discover`
  names all of them except TRAC, with the working board URL, rather than
  saying "nothing found". The detail is in
  [docs/PLATFORMS.md](docs/PLATFORMS.md).
- **Employers who block automated requests.** Tesco's careers site answers 403
  from Akamai, Sainsbury's replies "You got banned permanently from this
  server", and `jobs.louisvuitton.com` answers 403. `discover` reports those
  as blocked and stops. Nothing here attempts to defeat bot protection. A
  blocked front door is not always a blocked employer, though: Tesco's board
  is an ordinary Avature board at `careers.tesco.com` and reads fine once it
  is on the list by hand.
- **LinkedIn rows are leads, not postings.** The public `jobs-guest` endpoint
  carries no description and no salary, so those roles cannot be screened
  against your dealbreakers, only looked at.
- **Salary you can filter on.** Around a third of postings state one, and
  where you are looking moves that a long way. Measured across the published
  seed, US postings state a figure 34% of the time, against 19% for the roles
  a UK reader gets. So a UK search is closer to one in five than one in
  three, and most of your list will read `unconfirmed salary`. That is the
  market, not a bug, and the salary rule above is built around it.
- **Currencies other than your own** are never converted. With a floor set, a
  salary in another currency is shown, marked "not compared", and can neither
  disqualify a role nor earn it points, because a wrong exchange rate drops
  real roles quietly. With no floor set nothing is compared at all, so nothing
  carries that mark.
- **The right to work.** A posting that states its sponsorship position is
  flagged, read from the description. Most state nothing, and nothing here is
  filtered on it, so treat an unflagged role as unknown rather than as
  available.
- **Sector filtering is only as good as the tags.** Of 17,815 sources, 6,093
  carry a sector tag and 11,722 carry none, because an address harvested from
  a crawl index does not say what industry the employer is in. A `sectors:`
  filter keeps every untagged source as well as the ones you asked for, so it
  never cuts you down to the tagged few, but you cannot ask this list for
  "every hospitality employer" and get a true answer.
- **Five adapters are unverified.** Recruitee, Personio and the generic RSS
  reader are marked best-effort: they parse, and they have never been checked
  against live data. Reed and Adzuna are a stronger caveat, because no key was
  ever obtained here, so **neither has made a successful keyed call**. Treat
  your first run of either as the test, and believe the run over the docs.
- **Ranking and drafting cost money.** `rank`, `generate` and the dashboard's
  screen, CV and cover letter buttons run headless `claude -p` and spend
  tokens. Nothing generates unless you click it.

---

## Under the hood

The four things here that were harder than they look.

**Failure usually looks like success.** Ashby answers HTTP 200 with an empty
array both for a board that does not exist and for one that is rate-limiting
you, so validation is on job count and never on the status code.
SmartRecruiters does the same. Workday answers **406, not 404**, for a tenant
that is not there, because of wildcard DNS on `*.myworkdayjobs.com`. Taleo
lies about paging twice: it echoes back a page size it ignored (ask for 100,
get 25 under a stated pageSize of 100), and a page past the end returns the
last page again rather than nothing, so a loop that stops on an empty page
never stops. Its `totalCount` overstates too, reporting 3 where Transport for
London serves 1. Jobvite 302s an unknown company onto a page with no rows, so
a redirect-following fetch turns "no such board" into an ordinary 200. All
twenty-three platforms have a row of their own in
[docs/PLATFORMS.md](docs/PLATFORMS.md).

**A board that answers is not the company you asked for.** Tokens rarely match
company names: `mymoose` is Rapid7, `evergreenix` is Garrison, `knowbe4` is
Egress. So `discover` reads the token off the careers page rather than
guessing it, and then checks the board's own claim about itself against the
domain you asked for. Ashby `primer` is a Florida micro-schools operator,
Greenhouse `peak` is a Texas physiotherapy chain, and both look exactly like a
working board until something compares the names. A mismatch is reported, not
filed, and `--add` refuses to write it.

**Per-host pacing, and a circuit breaker.** Concurrency (how many different
boards are read at once) defaults to 16 and is capped at 64, but that number
governs breadth, not how hard any one host is hit: each host is paced on its
own clock, roughly 3 requests a second, slower for the strict ones (Workable's
is 0.7, learned from throwing 250 live employers away in one run by outrunning
it). Requests to different hosts are interleaved rather than sent in the order
the source list happens to store them, so 4,100 consecutive Greenhouse entries
do not park the whole pool on one host while everything else waits. A host
that answers three different sources with 429 in a row, having already used up
their retries, is treated as saying no rather than asking for a pause: it is
blocked outright for five minutes rather than retried into. A `Retry-After`
under a minute is honoured as a pause; over a minute it is read as a refusal
for the rest of the run. The user agent identifies the tool and links here.
These are other people's servers, and a job board that starts blocking
scrapers makes the market worse for everyone using this.

**The advert is usually not in the list.** Most boards return a summary, so
`enrich` fetches the posting itself, and every platform hides it somewhere
else: Taleo puts it in one URL-encoded entry of an `api.fillList` array whose
index moves per career section, iCIMS needs `in_iframe=1` on the posting page
as well as on the search, and SuccessFactors nests spans inside the
description span, so PSEG's 15,758 character advert comes back as 121
characters to a lazy regex and the closing tag has to be found by counting.
Which system publishes the advert is not always which board it came off
either, so `enrich` tries the platform's own reader first and then the shape
of the role's own URL: of 1,882 Phenom roles measured, 1,562 point at a
Workday tenant, 73 at iCIMS and 33 at a SuccessFactors host.

### A job posting is hostile input

Descriptions come from thousands of third-party servers, and anyone can post a
job to a job board. That text ends up in two places that matter: a prompt, and
the working directory of a subprocess that can write files. So it is treated
as hostile input rather than as content.

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

## robots.txt, and bot protection

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
`titles.include` rather than being fixed. They are public pages served without
a login, and this reads them at a handful of requests per run rather than at
crawl scale, but that does not make it permitted.

`enrich` then fetches the full posting for each LinkedIn result it kept, one
request per role, on the same endpoint family and under the same caveat. That
is the larger share of the traffic, not the searches.

**What that means for you.** LinkedIn may block the IP you run this from, and
they take a harder line on scraping than most. If that matters to you, delete
the single entry with `"platform": "linkedin"` from `sources/sources.json`, or
set `sources.use_bundled: false` and list your own. `scan --no-enrich` turns
off the per-role fetch on its own. Everything else in the tool works without
any of it.

Reed and Adzuna are the two keyed sources you can add yourself, and both are
off by default. Where their robots.txt and their terms sit against each other,
in full, is in [docs/SOURCES.md](docs/SOURCES.md).

If you are running this at work, on shared infrastructure, or anywhere the
consequences are not purely yours, read that table before you press go.

---

## Where the source list comes from

The list is data, and data rots: boards migrate between applicant tracking
systems, tokens get renamed, companies get acquired. One revalidation pass
found 23 dead boards, and 19 of those had simply moved and were hiding 762
live roles the scan could no longer see. A weekly job in this repository
revalidates every board on Sunday mornings and opens a pull request pruning
anything dead, so `git pull` is what keeps a clone current. Growing the list
is a separate job that does not live here.

[docs/SOURCES.md](docs/SOURCES.md) is the full account: why employer boards
rather than aggregators, the two aggregators that are in and how to key them,
every well-known job board that was checked and rejected and what it answered,
and what the source list holds.

---

## Skills

`skills/` holds Claude Code skills that pair with the scanner. `rate-cv` also
ships as [its own repository](https://github.com/maccydee/rate-cv), which is
the source of truth; the copy here is synced weekly by a workflow. See
[skills/README.md](skills/README.md).

---

## Development

```bash
python3 -m pytest -q            # the whole suite
python3 tests/run_all.py        # the same suite, without pytest, and what CI runs
job-radar validate --file sources/sources.json --report out/validation.json
job-radar coverage              # what the source list actually holds
```

`tests/run_all.py` discovers every `tests/test_*.py` and needs nothing
installed, pytest included. It is what CI runs, and naming one file instead is
the mistake it exists to prevent: CI ran `tests/test_core.py` and nothing
else, so `tests/test_locations.py`, which holds every country-code rule that
decides whether a job is one you can legally take, had never executed once.
It prints its own total at the end, so the count lives in the run rather than
in this file, and a new `test_*.py` runs here and in CI without anyone editing
a workflow.

The suite has under-reported itself two other ways, both fixed and both worth
knowing before you add to it. The `__main__` block that collects `globals()`
once sat partway up `test_core.py`, so it saw only the tests defined above it
and ran less than half the file while printing what looked like a full pass;
it has to stay at the end now, and the file's own comment says why. And the
runner caught `Exception` rather than `BaseException`, so one test raising
`SystemExit` ended the whole run mid-file with no failure line and no summary.

MIT licensed.
