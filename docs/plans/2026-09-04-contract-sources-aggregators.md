# Turning on Reed and Adzuna for UK contract work

Research only. Nothing in the repo was edited or run; no account was created;
no key was requested. This is what it would take, and what would happen the
day after.

## 1. Reed

**Signup.** <https://www.reed.co.uk/developers/jobseeker> — a three-field form
(first name, last name, email), the key is emailed to you. No card, no paid
tier documented anywhere on that page or on the general
<https://www.reed.co.uk/developers> listing. `docs/SOURCES.md:29-31` already
says this and it matches what the page itself states.

**Rate/volume limits.** Not documented publicly. I fetched
`reed.co.uk/developers/jobseeker` and `reed.co.uk/developers` directly and
searched for "free", "limit", "quota", "per day/minute/hour", "throttle" — none
appear on either page. Third-party wrapper repos (`Ara225/reed-jobseeker-api`)
don't state one either; they just point back at the same docs page. **I could
not determine Reed's rate limit and did not find one stated anywhere Reed
itself publishes.** `fetch.py` paces it through the shared per-host limiter
rather than a documented number (`fetch.PER_HOST_RPS`), which is the codebase
already treating this as unknown.

**Auth.** The key is the HTTP Basic username, password left empty. `fetch_reed`
(`jobradar/fetch.py:1155-1229`) does exactly this: sets `session.auth = (api_key, "")`
on the thread's shared `requests.Session`, and restores it in a `finally` —
the comment there explains why (a session-wide default that isn't restored
would leak the key into the Authorization header of every other request that
thread makes for the rest of the scan, which is a real third-party privacy
problem, not a style nit).

**Key supply to job-radar.** `sources.reed_api_key` in `config.local.yaml`, or
the `REED_API_KEY` environment variable — `config.py:852` and `_api_key()`
(`config.py:545`) read the env var only when the config value is blank, config
wins. With neither set, `fetch_reed` returns a `Result` with a stated error
message rather than attempting the call (`fetch.py:1176-1180`), and `validate`
is specifically tested not to call a keyless Reed "dead" (`test_core.py:3959`).

**Documented query parameters (confirmed live off the docs page):**
`keywords`, `locationName`, `distanceFromLocation`, `minimumSalary`,
`maximumSalary`, `employerId`, `employerProfileId`, `postedByRecruitmentAgency`,
`postedByDirectEmployer`, `graduate`, `resultsToTake` (max 100),
`resultsToSkip`, and — the ones this task is about —
**`permanent`, `contract`, `temp`, `fullTime`, `partTime`**, all `"true"/"false"`
strings. The docs don't say whether combining `contract=true&temp=true` ANDs
or ORs them; that would need a live call to confirm.

**Source entry to add**, contract-and-interim-scoped, direct employers only,
following the same `{keyword}` keyword-template pattern the shipped Reed entry
and the existing LinkedIn contract entry (`sources/sources.json`, company
`"LinkedIn contract"`, `f_JT=C`) both already use:

```yaml
sources:
  reed_api_key: ""        # config.local.yaml (gitignored), or REED_API_KEY
  extra:
    - company: Reed contract
      url: "https://www.reed.co.uk/api/1.0/search?keywords={keyword}&contract=true&temp=true&postedByDirectEmployer=true"
      platform: reed
      country: UK
      keyword_template: true
```

Whether `&temp=true` belongs alongside `&contract=true` or should be its own
separate source is exactly the ambiguity above — worth one live call each way
before shipping, not a guess.

## 2. Adzuna

**Signup.** <https://developer.adzuna.com/signup> — username, email, password,
plus organisation name, website, intended use, monthly visitor estimate,
market region and industry sector, and acceptance of the Terms and Conditions
(confirmed by fetching the signup page directly). No payment method field on
that page. `docs/SOURCES.md:104-106` says "free, no card" and nothing in the
signup form or the ToS contradicts that for personal use.

**Rate/volume limits — confirmed from Adzuna's own Terms of Service**
(<https://developer.adzuna.com/docs/terms_of_service>): **25 hits/minute, 250
hits/day, 1,000 hits/week, 2,500 hits/month** on the default free tier. Same
numbers `docs/SOURCES.md:122-127` already states and does the arithmetic on
(twelve titles × 3 pages = 36 calls/scan, so the monthly cap of 2,500 is the
binding one — roughly two sustained scans a day). One thing the repo's docs
don't currently say: **the ToS also states a 14-day trial period for
commercial, government or academic use**, after which those uses need a
separate licence agreement. Personal-use job hunting is explicitly one of the
listed permitted uses with no such time limit, so this doesn't block Callum,
but it's worth knowing it exists.

**Auth.** `app_id` and `app_key`, both **in the query string** — Adzuna has no
header auth option (`fetch.py:1259-1265`, confirmed against the docs). Because
of that, `fetch_adzuna` deliberately builds the credentialled URL onto a
throwaway probe `Source` and returns the *original* uncredentialled source in
every `Result`, so the key never lands in `state.json` or the published source
list (`fetch.py:1268-1273`).

**Key supply.** `sources.adzuna_app_id` / `sources.adzuna_app_key` in
`config.local.yaml`, or `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` env vars — same
config-wins-over-env pattern as Reed (`config.py:853-854`). No credentials ⇒
stated error, not a fetch attempt (`fetch.py:1275-1280`), and `validate` is
tested not to call a keyless Adzuna dead either (`test_core.py:4351`).

**Documented query parameters** — I pulled these from Adzuna's own machine-readable
spec, `https://developer.adzuna.com/api_docs/services/236708.json` (the exact
URL `test_core.py:3990-3991` cites as its own source, read there on
2026-08-24; I re-fetched it now). The employment-type filters are:

- `full_time=1` — only full-time jobs
- `part_time=1` — only part-time jobs
- **`contract=1`** — only contract jobs
- `permanent=1` — only permanent jobs

plus the search params already in use: `title_only`, `results_per_page`,
`app_id`, `app_key`, `what`, `where`, `salary_min`, `sort_by`, etc.

**Source entry to add**, contract-scoped:

```yaml
sources:
  adzuna_app_id: ""       # config.local.yaml (gitignored), or ADZUNA_APP_ID
  adzuna_app_key: ""      # config.local.yaml (gitignored), or ADZUNA_APP_KEY
  extra:
    - company: Adzuna contract
      url: "https://api.adzuna.com/v1/api/jobs/gb/search/1?title_only={keyword}&contract=1&results_per_page=50"
      platform: adzuna
      country: UK
      keyword_template: true
```

## 3. Does job-radar's own adapter code carry the contract signal through?

**Reed: no, and it structurally can't from the data it currently reads.**
`parse_reed` (`jobradar/adapters/platforms.py:2790-2879`) reads `jobTitle`,
`jobUrl`/`jobId`, `jobDescription`/`description`, `locationName`,
`employerName`, `date`/`datePosted`, `expirationDate` — never `jobType` or a
contract flag of any kind. That isn't a bug in the parser: Reed's **search**
endpoint (the only one job-radar ever calls — the per-job **details** endpoint
that carries `salaryType`/`yearlyMinimumSalary` is never fetched anywhere in
the code, only exercised in isolated unit tests of `from_reed`) genuinely does
not return a per-result contract/permanent field at all. The `contract`/`temp`
parameters are **request-side filters only** — you scope the *search*, you
don't get told per-row which kind you got. So for Reed the only two levers are
(a) the query-scoped source above, and (b) the text classifier on title +
description.

**Adzuna: yes it's read, but it's thrown away for the one field that matters.**
Adzuna's response carries `contract_type` (`"permanent"` / `"contract"`) and
`contract_time` (`"full_time"` / `"part_time"`) per result — confirmed in
Adzuna's own docs and in the `ADZUNA_SEARCH` fixture in `test_core.py:4020-4021`,
itself sourced from Adzuna's OpenAPI spec. `parse_adzuna`
(`jobradar/adapters/platforms.py:3025-3029`) does read them:

```python
if str(j.get("contract_type") or "").lower() == "contract":
    job.flags.append("contract, not permanent")
if str(j.get("contract_time") or "").lower() == "part_time":
    job.flags.append("part time")
```

But that's it — a **string appended to `job.flags`**, a free-text list of
notes. It never reaches `job.employment`, which is the field everything
downstream actually filters and displays on
(`jobradar/cli.py:2190`, `emp = j.employment or "unstated"`). `job.employment`
is set exactly once, in `screen.enrich()` (`jobradar/screen.py:1387`):

```python
job.employment, _ev = employment.classify(job.title, job.description)
```

purely from title + description text, with no reference to `job.platform`,
`job.flags`, or anything Adzuna already told it. So today: an Adzuna posting
correctly flagged internally as `contract_type: contract` gets a
"contract, not permanent" note in its flags **and simultaneously** gets
whatever `employment.classify` decides from the (500-character-truncated,
agency-written) title and description — which, per `employment.py`'s own
design doc, defaults to `unstated` for the "overwhelming majority" of
postings, because contract language in a short agency blurb often doesn't hit
any of the decisive phrases. **The two can and will disagree on the same
posting**, and the one that's wrong (`job.employment`) is the one the
dashboard filter and facet counts actually use.

## 4. Will `employment.classify` work on aggregator text, and should the platform flag win?

**Partial credit, and the shortfall is structural, not a tuning problem.**

`employment.classify` (`jobradar/employment.py`) is not purely title-only in
implementation, despite the module's framing — the `_DECISIVE` pattern (IR35,
"fixed term contract", a duration + "contract/assignment/FTC", a day rate with
money next to it, "contract position/role/basis/...", "umbrella
company/payroll", "FTC" next to a duration) is explicitly checked against
**both** title and description (`classify()`, step 2, `jobradar/employment.py`
lines under `# 2. The description`). Only the looser, ordinary-English words
("contract", "contractor", "freelance", "temp", "interim", "fractional",
bare "day rate") are restricted to the title, because those are the ones the
module's own corpus study (773 false positives down to ~36 real ones) showed
are unsafe in a description.

For Reed and Adzuna specifically:

- **Titles work fine.** Aggregator titles are agency-written but they are
  *sales copy for the vacancy*, and agencies lean on exactly the words
  `_TITLE_ONLY` and `_DECISIVE` already catch — "Interim Head of
  Engineering", "12 Month FTC", "Contract - Outside IR35" are all real
  examples already in `tests/test_employment.py`'s own title corpus. No reason
  to expect Reed/Adzuna titles to behave differently from the ATS titles the
  classifier was tuned on; if anything agency titles state the engagement
  type *more* often, because rate and duration are the sales pitch.
- **Descriptions are the weak point, for a reason specific to aggregators.**
  Adzuna's description is hard-truncated to 500 characters by Adzuna itself,
  and Reed's, while not code-truncated, is often short and agency-boilerplate
  ("Great opportunity, apply now, immediate start"). The `_DECISIVE` phrases
  that do work in a description (IR35, a day rate with a currency symbol, "X
  months contract") are the ones most likely to appear early in a short
  agency blurb — so this is not hopeless — but it's a materially smaller
  surface than a full ATS posting, and nothing in the existing test suite
  exercises Reed/Adzuna fixtures against `employment.classify` at all (see
  §6). This is an untested assumption, not a proven one.

**Verdict: yes, the platform's own flag should be preferred over the text
classifier when the platform supplies one — for Adzuna today, and it should
be added for Reed only if the contract-scoped query source above is shipped.**
Adzuna's `contract_type` is a structured field the advertiser (or Adzuna's own
categorisation) set deliberately; the text classifier is a best-effort
fallback for when nobody tells you. Preferring a stated fact over an inference
made from a truncated 500-character snippet is the same principle already
governing every other "confirmed vs. guessed" decision in this codebase
(`Salary.confirmed`, `salary_is_predicted`, etc.) — it would be inconsistent
*not* to apply it here.

**Where the plumbing goes, concretely:**

1. `parse_adzuna` already has `j.get("contract_type")` and `j.get("contract_time")`
   in hand at the point it builds the flags (`platforms.py:3025-3029`). Carry
   the raw value onto the `Job` there — either as a new field
   (`Job.platform_employment: str | None`, alongside the existing
   `employment: str = "unstated"` field in `models.py:140`) or, more minimally,
   keep using `job.flags` but make its contents machine-readable rather than
   only human prose.
2. In `screen.enrich()` (`screen.py:1387`, immediately before the existing
   `employment.classify` call), check that field first: if the platform
   stated `contract` or `permanent`, set `job.employment` and evidence from
   that directly; only fall through to `employment.classify(title, description)`
   when the platform said nothing (which happens — `contract_type` isn't
   always populated) or for every non-Adzuna platform, where the field simply
   won't exist. This mirrors the precedence Reed's own salary handling already
   uses ("Reed's figures beat ours" — `salary.py:774`, tested at
   `test_core.py:3760`): prefer the platform's own stated answer over the
   inference when both exist.
3. For Reed, there is nothing to plumb from the payload (§3) — the equivalent
   move is shipping the `contract=true` query-scoped source in §1, which
   makes the *source* itself the "platform flag," at the cost of needing a
   second Reed source/keyword-budget entry rather than a single field read.

**Risk of getting it wrong.** Two distinct risks, and they cut in opposite
directions:

- **Trusting it blindly, unverified.** No live Reed or Adzuna call has ever
  been made from this repo (`verified=False` on both `Platform` entries,
  `adapters/__init__.py:408, 441`, and every existing Reed/Adzuna test is
  built from documented shapes, not real payloads — the comments on
  `REED_SEARCH` and `ADZUNA_SEARCH` in `test_core.py` both say so explicitly).
  Shipping a hard override of `job.employment` from a field nobody has ever
  seen real values for is exactly this repo's signature bug: a plausible
  mechanism that silently mislabels every row it touches and nothing about
  the output looks wrong. It needs a real payload sample before it ships,
  the same "prove every field" bar `CLAUDE.md`'s adapter section already sets
  for any new field a parser starts reading.
- **Not trusting it, keeping only the text classifier.** As shown in §3, the
  status quo already computes both signals and silently prefers the
  text-only one for the field that's actually filterable, which is worse: it
  isn't "cautious," it's discarding a stated fact in favour of a guess and
  giving no indication that happened. `unstated`/wrong-because-truncated is
  not a safer default than a documented platform field when the two disagree
  — it just fails quietly instead of loudly, which is the whole pattern
  `CLAUDE.md` names as the project's recurring defect.

The corollary from `employment.py`'s own docstring — "being conservative here
is close to free, a missed permanent falls to `unstated`, which is honest" —
does **not** transfer cleanly to overriding with a platform flag: a missed
platform-`contract` role that falls to text-`unstated` is not free, because
`unstated` is 97.8% of the board and drowns exactly the roles this whole
effort exists to surface.

## 5. Salary: does a day rate survive, worked example

**`Salary.annualised()`** (`jobradar/models.py:37-50`) multiplies a `period="day"`
top figure by 220 working days (`t * working_days`), an `hour` figure by
`220 * 8`. It trusts `self.period` completely — it has no independent way to
tell a day rate from an annual figure; that job is entirely `from_reed` /
`from_adzuna` / `parse_text`'s to get right before the `Salary` is built.

**Both `from_reed` and `from_adzuna` get it right, via the same two-stage
mechanism, and it's genuinely well-tested (`_REED_MIN_ANNUAL` /
`_ADZUNA_MIN_ANNUAL` = 2000.0 for both):**

1. The raw numeric fields (`minimumSalary`/`maximumSalary` for Reed,
   `salary_min`/`salary_max` for Adzuna) carry **no period** on the endpoints
   job-radar actually calls. A figure under £2,000 is refused as an annual
   salary and returned **unconfirmed** rather than annualised on a guess
   (`salary.py:814-818` for Reed, `:899-905` for Adzuna) — this is the
   `_MIN_ANNUAL` threshold the brief asked about, and it exists specifically
   so a `650` day rate can't be silently read as £650/year.
2. `parse_reed` / `parse_adzuna` then take a **second pass at the advert
   text** with `parse_text(desc, default_currency=...)`
   (`platforms.py:2838-2844` for Reed, `:2989-2995` for Adzuna) — if the
   description states "£650 - £700 per day", `parse_text`'s rate-family regex
   (`_RANGE_RATE`/`_SINGLE_RATE` in `salary.py`) picks it up, sets
   `period="day"`, `confirmed=True`, and that result **replaces** the
   unconfirmed one.

**Worked example, exactly as `tests/test_core.py:3733-3757` asserts it (Reed):**

```
Payload:  minimumSalary=650.0, maximumSalary=700.0, currency="GBP"
Advert:   "Interim engineering manager, £650 - £700 per day, outside IR35."

from_reed() alone           -> confirmed=False (700 < £2,000 floor)
parse_reed() full pipeline  -> job.salary.confirmed == True
                                job.salary.period == "day"
                                job.salary.max == 700
                                job.salary.annualised() == 700 * 220 == 154,000
                                clears_floor(job.salary, 140000, "GBP") == True
```

So a £650-700/day contract correctly clears a £140k floor as £154,000/year,
end to end, **provided the advert text states the rate explicitly** ("per
day"/"day rate"/a currency symbol next to "/day" — the patterns
`employment.py`'s own `_DECISIVE` regex and `salary.py`'s `_period()` both
recognise). This is exactly the case tested for Reed.

**What's unverified: the same mechanism for Adzuna specifically.**
`test_an_unlabelled_adzuna_day_rate_is_not_read_as_an_annual_salary`
(`test_core.py:4139-4152`) only proves step 1 (the unconfirmed floor) for
Adzuna — there is no equivalent end-to-end test proving `parse_adzuna`'s
second-pass `parse_text(desc, ...)` actually flips an Adzuna day-rate advert
to `confirmed=True, period="day"` the way the Reed one does. The code path is
identical (`platforms.py:2989-2995` mirrors `:2838-2844` almost exactly), so
I'd expect it to work the same way, but "the code looks the same" is not the
bar this repo sets for a salary field — `CLAUDE.md`'s adapter section
specifically wants a fixture-based assertion per field, and this one is
missing. Given Adzuna's description is truncated to 500 characters, whether
the day-rate phrase survives the truncation is exactly the kind of thing that
needs a real trimmed payload rather than an assumption.

**What happens if the advert never restates the rate in words** (bare
numeric field, nothing in the text): the role stays `confirmed=False` on both
platforms, is shown to the reader with an unconfirmed-salary label, and — per
`salary.clears_floor` and the tests above — **can never be disqualified by
the salary floor**. That's the deliberately safe failure mode, and it's
correct: it costs nothing but an "unconfirmed" label, never a silently
dropped role.

## 6. Things found broken, silent, or worth flagging as this repo's signature bug

1. **The one real "renders identically to success" risk: Adzuna's
   `contract_type` is read, flagged in prose, and then ignored for the field
   that's actually filterable (§3, §4).** Nothing errors, nothing looks
   wrong — the dashboard shows a "contract, not permanent" note right next to
   an `employment: unstated` facet on the same row, and neither the code nor
   any test currently asserts the two should agree. A reader filtering the
   board by employment type gets a result set built from the weaker signal
   while the stronger one sits unused three lines above it in the same
   function.
2. **Neither adapter has a dedicated fixture test file**, unlike every other
   adapter `CLAUDE.md`'s "Adding or changing an adapter" section describes
   (`tests/test_pcsx_adapter.py` is named as the shape to copy). Coverage
   exists — and it's substantial, ~30 tests across both in `test_core.py`
   (search "reed"/"adzuna", case-insensitive) — but it's folded into the
   general test file rather than living in `tests/fixtures/` the way the rule
   asks, and both `REED_SEARCH` and `ADZUNA_SEARCH` fixtures are built from
   *documentation*, not a real trimmed payload, because — as both docstrings
   say outright — no key was ever created to pull one. That's stated
   honestly in the code, not hidden, but it means every assertion in this
   report about Reed/Adzuna's field shapes rests one level removed from a
   real response.
3. **The Adzuna day-rate second-pass has no end-to-end test** (§5) — the Reed
   equivalent does. Not a bug, a coverage gap in exactly the place this
   repo's own rules say needs a test before being trusted.
4. **Reed's `contract=true`/`temp=true` combination behaviour is undocumented
   by Reed** (§1) — whether they AND or OR is unknown until tried live.
   Shipping both on one source without checking risks a source that silently
   returns zero results forever, which — per this repo's own definition of
   its signature bug — is indistinguishable from "no contract roles today"
   unless someone happens to compare it against `contract=true` alone.
5. **Not a bug, but worth being explicit about given the brief's framing**:
   `employment.py`'s docstring says the design "must" read loose words from
   the title only, and frames this as the whole rule. In the actual
   implementation the `_DECISIVE` set (which includes IR35, day-rate-with-
   money, and "months + FTC" — exactly the phrases most likely in a short
   agency blurb) already runs against the description too. That's good news
   for aggregator coverage and is worth knowing before assuming the
   classifier is stricter than it is.

## What I could not determine

- Reed's numeric rate limit (requests/second, /day, or /month) — not
  published anywhere on `reed.co.uk/developers*` or in the third-party
  wrapper repos checked.
- Whether Reed's `contract=true` and `temp=true` can be combined in one
  query, and whether that ANDs or ORs.
- Whether Adzuna's day-rate second-pass (`parse_text` on the truncated
  description) actually confirms a day rate the way Reed's does — the code
  path is identical but untested for Adzuna specifically.
- Whether real Reed/Adzuna payloads match the documented shapes the existing
  tests are built from — nobody has made a live call against either API from
  this repo.

No account was created and no signup form was submitted for either provider.
