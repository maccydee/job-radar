# Config reference

Every setting, what it accepts, and what happens when it is wrong. The file is
`config.local.yaml` if present, otherwise `config.yaml`. `job-radar setup`
writes one for you; this is for editing it afterwards, or writing it by hand.

**The config is validated when it loads.** An unknown key, a broken regex, a
salary that is not a number or a format that does not exist stops the run with
a message naming the setting. It does not silently do something else.

## titles

| key | type | notes |
|---|---|---|
| `include` | list of strings | **Required.** Matched against the posting title, whole words, case-insensitive. Also the search terms for NHS Jobs and LinkedIn, so wrong titles there return nothing rather than merely filtering loosely. Only the **first six** are used as search terms. |
| `exclude` | list of strings | Never show these, even when `include` matches. Escaped, so brackets and punctuation are safe: `healthcare assistant (bank)` works. |

An empty `include` is refused: with no titles every posting matches and the
keyword sources have nothing to look for.

## locations

| key | type | notes |
|---|---|---|
| `countries` | list of codes | Empty means anywhere. Country names are accepted and normalised (`Portugal` becomes `PT`); anything the filter cannot use is refused at load rather than silently matching nothing. Note the UK is `UK`, not `GB`. |
| `remote_ok` | true / false | Unquoted. `"no"` and `"false"` are understood, anything else is refused rather than read as true. |
| `relocate_to` | list of codes | Shown, scored below home. Same validation as `countries`. |

The full set of codes the location filter recognises:

`AE`, `AR`, `AT`, `AU`, `BE`, `BR`, `CA`, `CH`, `CN`, `CZ`, `DE`, `DK`, `ES`, `FI`, `FR`, `HK`, `ID`, `IE`, `IL`, `IN`, `IT`, `JP`, `KR`, `MX`, `MY`, `NL`, `NO`, `NZ`, `PH`, `PL`, `PT`, `RO`, `SE`, `SG`, `TH`, `TR`, `UK`, `US`, `VN`, `ZA`

A country not on this list cannot be filtered on. Roles there are still
fetched; they are dropped as "location not recognised" unless `countries` is
empty.
| `exclude` | list of places | Applied per location. A role in London only is dropped; a role in "London / Manchester" survives on Manchester. |

## cv

| key | type | notes |
|---|---|---|
| `path` | path | **Required for document generation.** `~` is expanded. Checked on every load, so a CV you moved fails loudly instead of producing an invented one. |

## salary

| key | type | notes |
|---|---|---|
| `currency` | `GBP`, `USD` or `EUR` | Anything else is refused. A salary in a different currency to your floor is never converted: it is shown and marked "not compared", and it can neither disqualify a role nor earn it points. |
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
actually exist in the bundled list:

| sector | sources |
|---|---|
| `technology` | 229 |
| `finance` | 75 |
| `healthcare` | 45 |
| `industry` | 43 |
| `professional-services` | 34 |
| `security` | 33 |
| `media` | 31 |
| `telecoms` | 26 |
| `public-sector` | 25 |
| `retail` | 24 |
| `education` | 24 |
| `legal` | 17 |
| `travel` | 16 |
| `hospitality` | 15 |
| `charity` | 15 |
| `untagged` | 1 |

A sector that is not in this table matches nothing, so you are left with only
the untagged sources. Check with `job-radar coverage`.

## sources

| key | type | notes |
|---|---|---|
| `use_bundled` | true / false | |
| `countries` | list of codes | Only filters sources that carry a country tag; most do not. |
| `extra` | list | Either a bare URL string, or `{company, url, platform}`. `job-radar discover <name> --add` writes these for you. |

## output

| key | type | notes |
|---|---|---|
| `formats` | list | `html`, `json`, `markdown`. Anything else is refused, rather than producing a successful run that writes no files. |
| `dir` | path | `~` is expanded. |

## fetch

| key | type | notes |
|---|---|---|
| `concurrency` | number | Default 4, capped at 12 with a warning. These are other people's servers. |
| `timeout` | seconds | Default 20. |
| `retries` | number | Default 2. |
| `user_agent` | string | Identifies the tool. Leave it identifying. |

## Command-line flags not in the examples

| flag | applies to | notes |
|---|---|---|
| `-c, --config` | all | Which config to load. |
| `--db` | scan, list, applied, generate, serve | Database path. Default `data/job-radar.db`. |
| `--docs` | generate, serve | Where generated documents go. Default `~/job-applications`. |
| `--limit` | scan, list | Cap the sources fetched, or the rows listed. |
| `--dry-run` | scan | Do not record what was seen. |
| `--json` | list | Machine-readable output. |
| `--all` | list | Include settled roles. |
| `--port`, `--host`, `--no-browser` | serve | |
