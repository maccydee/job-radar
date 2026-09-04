# Config reference

Every setting, what it accepts, and what happens when it is wrong. The file is
`config.local.yaml` if present, otherwise `config.yaml`, unless `-c` or the
`JOB_RADAR_CONFIG` environment variable names one. Both filenames are
gitignored, so the repo ships neither and a fork using GitHub Actions has to
`git add -f config.yaml`. `job-radar setup` writes one for you; this is for
editing it afterwards, or writing it by hand.

**The config is validated when it loads.** An unknown key, a broken regex, a
salary that is not a number or a format that does not exist stops the run with
a message naming the setting. It does not silently do something else.

## titles

| key | type | notes |
|---|---|---|
| `include` | list of strings | **Required.** Matched against the posting title, whole words, case-insensitive. Also the search terms for the keyword sources (NHS Jobs, LinkedIn, the Workable search, and Reed and Adzuna if you add them), so wrong titles there return nothing rather than merely filtering loosely. Only the **first twelve** are used as search terms, and a scan names any beyond that rather than dropping them silently. A title the regex misses gets a second pass from a looser matcher, which accepts the same words in another order with up to two words between them, so `engineering manager` also finds "Manager, Engineering Platform". A word that changes the job rather than rewording it (thirty of them, "product", "business", "program" and "sales" among them) still blocks it, so "Engineering Program Manager" does not pass. The loose pass runs after the regex, never instead of it. |
| `exclude` | list of strings | Never show these, even when `include` matches. Escaped, so brackets and punctuation are safe: `healthcare assistant (bank)` works. |

An empty `include` is refused: with no titles every posting matches and the
keyword sources have nothing to look for.

## locations

| key | type | notes |
|---|---|---|
| `countries` | list of codes | Empty means anywhere. Country names are accepted and normalised (`Portugal` becomes `PT`); anything the filter cannot use is refused at load rather than silently matching nothing. Note the UK is `UK`, not `GB`. |
| `remote_ok` | true / false | Unquoted. `"no"` and `"false"` are understood, anything else is refused rather than read as true. |
| `work_modes` | list | Keep only these arrangements: `remote`, `hybrid`, `office`. Empty (the default) keeps all. A posting that states no arrangement is always kept and flagged, because half of them state none and "we cannot tell" is not "not remote". `remote_ok` cannot express "remote only"; this can. |
| `relocate_to` | list of codes | Shown, scored below home. Same validation as `countries`. Also the countries the Workable search is run against, alongside `countries`. |
| `need_sponsorship` | list of codes | Where you would need a visa. Same validation as `countries`. A role in one of these is hidden only if the posting says outright that it will not sponsor; one that says it will scores higher, and one that says nothing is kept and left as a question to ask. |
| `exclude` | list of places | Applied per location. A role in London only is dropped; a role in "London / Manchester" survives on Manchester. |

The full set of codes the location filter recognises:

`AE`, `AR`, `AT`, `AU`, `BE`, `BR`, `CA`, `CH`, `CN`, `CZ`, `DE`, `DK`, `ES`, `FI`, `FR`, `HK`, `ID`, `IE`, `IL`, `IN`, `IT`, `JP`, `KR`, `MX`, `MY`, `NL`, `NO`, `NZ`, `PH`, `PL`, `PT`, `RO`, `SE`, `SG`, `TH`, `TR`, `UK`, `US`, `VN`, `ZA`

A country not on this list cannot be filtered on. Roles there are still
fetched; they are dropped as "location not recognised" unless `countries` is
empty.

## cv

| key | type | notes |
|---|---|---|
| `path` | path | **Required for document generation.** `~` is expanded. Checked on every load, so a CV you moved fails loudly instead of producing an invented one. |

## salary

| key | type | notes |
|---|---|---|
| `currency` | any of the 45 the parser reads | Anything else is refused; the list is `salary.KNOWN_CURRENCIES`, built from the parser's own tables so the two cannot drift. A salary in a different currency to your floor is never converted: it is shown and marked "not compared", and it can neither disqualify a role nor earn it points. With no floor set nothing is compared, so nothing carries that mark. |
| `floor` | number | `70000`. `£70,000` and `70,000` are accepted and converted. Words are refused. A role whose **stated** pay is below this is hidden; a role with **no** stated pay is always shown and marked. |

## dealbreakers

A list of `{name, pattern, hard}`. `pattern` is a regular expression read
against the job description. `hard: true` hides the role, `hard: false` shows
it with a warning.

Every entry is validated: a missing pattern, an unknown key, or a regex that
does not compile stops the run and names the entry. Previously these were
dropped in silence, so a dealbreaker you thought was protecting you was simply
absent.

## sectors

Which employers to watch. Empty means all of them. These are the tags that
actually exist in the bundled list, out of 17,814 sources:

| sector | sources |
|---|---|
| `untagged` | 11,722 |
| `healthcare` | 1,311 |
| `finance` | 1,304 |
| `education` | 512 |
| `media` | 498 |
| `energy` | 409 |
| `retail` | 407 |
| `technology` | 409 |
| `construction` | 311 |
| `transport` | 239 |
| `telecoms` | 224 |
| `public-sector` | 161 |
| `hospitality` | 74 |
| `charity` | 65 |
| `legal` | 43 |
| `industry` | 42 |
| `security` | 34 |
| `professional-services` | 33 |
| `travel` | 16 |

**Read the first row before setting this.** Only 6,092 sources carry a tag at
all. The rest arrived from a crawl-index harvest that knows a board's address
and not the employer's industry, so `healthcare` being 1,311 is a count of
labels, not a count of healthcare employers, and `public-sector` in
particular catches a lot of noise a name-based rule cannot filter out (US
municipal and non-profit employers as often as UK public bodies).

Setting `sectors` **keeps every untagged source as well** as the ones tagged
with what you asked for. So it removes the labelled sources you did not ask
for and leaves the other 11,722 in place, which is why it narrows the list far
less than the numbers above suggest. A tag that is not in this table is
refused at load rather than quietly matching nothing. Check yours with
`job-radar coverage`, which counts the file rather than this table.

## sources

| key | type | notes |
|---|---|---|
| `use_bundled` | true / false | |
| `countries` | list of codes | Only drops a source whose country tag names somewhere else, which is 10,068 of the 17,814. The other 7,746 are fetched whatever you set: 5,215 carry no tag, and 1,597 are tagged `multi`, meaning the board belongs to a multinational rather than to one country. Neither is evidence the employer has nothing where you are, and a multinational is one of the likelier places to find a vacancy in your country, so both are kept. `[UK]` therefore leaves 7,746 sources rather than the 934 tagged `UK`. This trims what gets fetched; `locations.countries` is what decides where a role is. |
| `extra` | list | Either a bare URL string, or `{company, url, platform}`. `job-radar discover <name> --add` writes these for you. |
| `reed_api_key` | string | Free key from <https://www.reed.co.uk/developers/jobseeker>, needed only if you add the Reed source. Falls back to the `REED_API_KEY` environment variable when blank, which is the route for GitHub Actions. **Put a real key in `config.local.yaml`, never in `config.yaml`**: the second one is the file a fork force-adds for GitHub Actions, so it is the one that ends up committed. Blank means the Reed source is skipped, with a message naming it. |
| `adzuna_app_id`, `adzuna_app_key` | string | Free pair from <https://developer.adzuna.com/signup>, needed only if you add the Adzuna source. Both fall back to `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` in the environment when blank, which is the route for GitHub Actions. **Real values go in `config.local.yaml`, never in `config.yaml`**, which is the file a fork force-adds for GitHub Actions. Either one missing means no credentials, and the Adzuna source is skipped with a message naming it. Adzuna's free limits are 25 calls a minute, 250 a day, 1,000 a week and 2,500 a month; one scan is one call per job title per page. |

## output

| key | type | notes |
|---|---|---|
| `formats` | list | `html`, `json`, `markdown` (`md` is accepted for it). Anything else is refused, rather than producing a successful run that writes no files. |
| `dir` | path | `~` is expanded. |

## fetch

| key | type | notes |
|---|---|---|
| `concurrency` | number | Default 16, capped at 64 with a warning. This governs how many DIFFERENT boards are read at once, not how hard any one host is hit: each host is paced separately (roughly 3 requests a second, slower for the strict ones), and a host that keeps refusing is blocked outright rather than retried into. How long a scan takes follows from this and from how many sources you keep, not from a fixed rate; a shorter list or a higher concurrency both move it. |
| `timeout` | seconds | Default 20. |
| `retries` | number | Default 2. |
| `user_agent` | string | Identifies the tool. Leave it identifying. **Accepted but not currently applied**: the loader validates the key and then does not read it, so every request goes out under the default agent. |

## Command-line flags not in the examples

| flag | applies to | notes |
|---|---|---|
| `-c, --config` | all | Which config to load. |
| `--db` | scan, enrich, rank, rescreen, list, applied, generate, serve | Database path. Default `data/job-radar.db`. Pointing a scan somewhere else isolates it: the one-off import of `state/seen.json` and `applications.local.yaml` follows the database, not the working directory, so `--db /tmp/scratch.db` does not copy your real history into a scratch file. |
| `--docs` | generate, serve | Where generated documents go. Default `~/job-applications`. |
| `--limit` | scan, validate, enrich, rank, rescreen, list | Cap the sources fetched or checked, the roles re-read, scored or listed. |
| `--dry-run` | scan, rank, enrich | Do not record what was seen, and do not write into `out/` either: a dry run leaves the last real dashboard where it is. On `rank`, show what it would cost and send nothing. |
| `--json` | list | Machine-readable output. |
| `--all` | list | Include settled roles, and roles no longer on a board. |
| `--new` | list | Only roles first seen on the most recent scan. |
| `--no-enrich` | scan | Skip fetching full postings for headline-only sources. They stay unscreenable. |
| `--prune`, `--force-prune` | validate | Rewrite `--file` without the dead sources. |
| `--refresh`, `--top` | rank | Re-score roles that already have a fit; how many to print. |
| `--remove` | rescreen | Delete the stored roles that no longer match your config. Off by default: `rescreen` reports and changes nothing without it, and a role you have already given a status is never removed whatever it matches. |
| `--port`, `--host`, `--no-browser` | serve | |
| `--defaults`, `--cv`, `--titles` | setup | `setup` asks questions and so refuses anything that is not a terminal. The scriptable form is `job-radar setup --defaults --cv PATH --titles "a,b"`; `--cv` is required with `--defaults`. Add `--scan` to run the first scan straight after. |
