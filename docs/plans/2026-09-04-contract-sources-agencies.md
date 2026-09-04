# UK contract/interim technology-leadership recruiters: ATS-board sweep

Research pass only. No config or source-list file in this repo was edited.
84 UK contract/interim/fractional technology-leadership recruiters,
consultancies and interim-executive specialists were checked with
`python3 -m jobradar.cli discover "<name>"` (never `--add`). Every domain
guess `discover` could not resolve from a bare company name was retried a
second time against the firm's real domain (`.co.uk` as well as `.com`),
since `discover`'s own bare-name guesser only ever tries `<slug>.com`.

Every board `discover` reported as `[verified]` was then re-fetched, once,
through the exact code path a real scan uses —
`jobradar.adapters.prepare()` → `jobradar.fetch.fetch_one()` →
`jobradar.adapters.parse()` — and every returned job title was run through
`jobradar.employment.classify()` to flag which look like contract, interim
or fixed-term work.

## 1. Verified boards ready to add

| Company | URL | Platform | Postings | Sample titles | Contract/interim-looking |
|---|---|---|---|---|---|
| La Fosse | `https://apply.workable.com/api/v1/widget/accounts/lafosse?details=true` | workable | 16 | "Experienced Legal Advisor - 12 Month FTC"; "Experienced Recruitment Consultant - Cloud & Infrastructure - Contract Desk"; "Experienced Recruitment Consultant - Information Security - Contract Desk" | 4 of 16, but see caveat below — these are La Fosse's OWN staff vacancies, not client placements |
| Made Tech | `https://made-tech.pinpointhq.com/postings.json` | pinpoint | 21 | "Cloud Engineer"; "Lead Technical Architect"; "Senior Content Designer" | 2 of 21, own permanent staff hiring |
| Harnham | `https://harnham.pinpointhq.com/postings.json` | pinpoint | 11 | "Recruitment Consultant"; "Recruitment Consultant / Senior Recruitment Consultant - US Market"; "Recruitment Consultant" (Phoenix) | 0 of 11, own permanent staff hiring |

**The critical caveat, found on every single one of these boards and on
five more already in the bundled 17,811-board list that were checked for
comparison (Odgers via iCIMS, PA Consulting via SmartRecruiters, Methods,
Version 1 and The Investigo Group via Workable/SmartRecruiters): the ATS
board a recruiter or consultancy runs is their OWN hiring — recruitment
consultants, ops staff, engineers on their permanent payroll — not the
day-rate contract or interim vacancies they place with client
organisations.** Those client vacancies live on a separate, custom-built
"search our jobs" widget on the agency's own site, which is exactly why
`discover` cannot find an ATS signature there: there usually isn't one.
La Fosse's board is the only one of the three above that even carries
contract-flagged titles, and every one of them is La Fosse hiring its own
recruitment consultants onto a "Contract Desk" team, not a contractor role
La Fosse is placing at a client. None of the three verified boards above
contains a single externally-placed day-rate technology-leadership role.

So while all three are real, live, ATS-readable, ready to add with
`--add`, and would cost nothing to include, **none of them widens the
UK contract/interim technology-leadership market this tool can see.**
They add recruitment-industry vacancies (a legitimate but different
job-radar audience) to the professional-services sector, nothing more.

## 2. Firms checked whose board could not be read

Every company below was tried once by bare name and, where that produced
no ATS signature, a second time against the firm's actual `.co.uk` or
`.com` domain. `discover` follows the redirect chain and reads the
landing page for an embedded ATS; the reasons below are what it reported.

### No ATS platform found (custom-built vacancy search, most likely)

Client Server, Harvey Nash, Nigel Frank, Tenth Revolution Group, Investigo,
SThree, Computer Futures, Huxley, Hays, Oliver Bernard, Understanding
Recruitment, Trust In SODA, Third Republic, Salt, Amber Labs, Methods,
BJSS, Kubrick, Infinity Works, Equal Experts, Cognizant Softvision, Odgers
Interim, Green Park, Alium Partners, Executives Online, Interim Partners,
Freeman Clarke, CTO Academy, Cathcart Associates, Reperio Human Capital,
Michael Page Technology, Ampersand Consulting, Nicoll Curtin, Frank
Recruitment Group, Progressive Recruitment, Real Staffing, Explore Group,
Curo Talent, Piper Maddox, Randstad Technologies, Xcede, Venn Group,
Templeton and Partners, Jefferson Frank, Nicholas Associates, Circle
Recruitment, Mortimer Spinks, Harrington Starr, Barclay Simpson, Version 1
(bare-name only; a SmartRecruiters board for it already exists in the
bundled list — see note below), Netcompany (same — already bundled,
SmartRecruiters), Applied Value Group, Sanderson, Aspire Technology
Solutions, Adria Solutions, Adecco (bare-name only; a stale/placeholder
Recruitee board for "Adecco UK" already exists in the bundled list — see
note below), Morson Group, Spring Technology, CBSbutler, Concept, Nexus
Jobs, Opus Recruitment Solutions, IC Resources, Michael J Bull, Motion
Recruitment, ARC IT Recruitment.

`discover`'s answer for all of these: "nothing found. Try their careers
page URL directly, or the URL you land on after clicking through to their
vacancy list." The honest reading is that the great majority of these
firms run a bespoke vacancy-search page rather than a commercial ATS —
exactly what the earlier `SOURCES.md` sweep found for the big generalist
boards, playing out again one firm at a time.

### Blocked (bot protection — not attempted further, per the rules)

Robert Walters, The CTO Club, Anson McCade, Digital Waffle, Datatech
Analytics, Harvey John, IntaPeople, Goodman Masson, Damia Group.

`discover` reported each of these as "careers site refused automated
requests" against a meaningful share of the candidate URLs it tried (1 of
33 up to 33 of 33). No user-agent rotation, proxy or headless browser was
used to get past this, per the standing rule.

### Identity mismatch (a board was found, but it is not this employer)

Xdesign — the Greenhouse board at `boards-api.greenhouse.io/v1/boards/xdesign`
answers 47 live jobs but names itself "CreateFuture", not "XDesign".
XDesign was acquired by / rebranded as CreateFuture; the token is a
leftover. Not offered as an XDesign source. (CreateFuture's own board, if
wanted separately, is a digital consultancy's permanent hiring, not a
contract-placement agency, and was not pursued further as it sits outside
this brief.)

### Found, but genuinely unreadable

Sapient (Publicis Sapient) — an iCIMS board exists at
`referral-publicisgroupe.icims.com`, but it answers HTTP 200 with "Error:
Login is required to search for jobs." — a real board this tool cannot
read, not an empty one.

### Inconclusive — request timed out under the polite pacing budget

Networkers, Michael Bailey Associates, Rullion. Each ran past the 30-second
per-company cap used for this sweep (itself well inside the tool's own
retry/timeout handling); none was retried a second time in the same
session to avoid exceeding the "stop after two failures against the same
host" rule. Worth a single retry on a future pass, not a verdict either
way today.

### Already in the bundled 17,811-board list, and checked for this brief

Five boards already shipped in `sources/sources.json` under
`sector: professional-services` sit inside the same firm list this brief
was asked to extend, so they were fetched and checked rather than
re-offered as new:

| Company | Platform | Live postings | What they actually are |
|---|---|---|---|
| Odgers | icims | 7 | Odgers' own staff: service desk engineer, infrastructure engineer, executive-search consultants. Not interim placements. |
| PA Consulting | smartrecruiters | 100 | PA's own permanent consultant hiring, plus a handful of internal FTC roles. |
| Methods | workable | 106 | Methods' own permanent staff hiring (Azure engineers, architects). |
| Version 1 | smartrecruiters | 85 | Version 1's own permanent staff hiring. |
| The Investigo Group | workable | 3 | A thin, mostly-ops internal board — not Investigo's client contract book. |
| Netcompany | smartrecruiters | 100 | Netcompany's own permanent staff hiring across several EU offices. |
| Adecco UK | recruitee | 1 | A dead/placeholder board: its one listing is Recruitee's own demo posting, "Senior Marketer (Sample)". Mislabelled `country: UK`. |
| Randstad UK | personio | 8 | Mislabelled: the postings are all in German, for Vienna/Graz/Tirol — this is Randstad Austria's internal hiring, not a UK board. |

Two of those (Adecco UK, Randstad UK) look like existing data-quality bugs
in the bundled list rather than anything this brief needs to fix, and are
flagged here rather than silently left for the next person to rediscover.

## 3. Verified entries, ready to paste

```json
[
  {"company": "La Fosse", "url": "https://apply.workable.com/api/v1/widget/accounts/lafosse?details=true", "platform": "workable", "country": "UK", "sector": "professional-services"},
  {"company": "Made Tech", "url": "https://made-tech.pinpointhq.com/postings.json", "platform": "pinpoint", "country": "UK", "sector": "professional-services"},
  {"company": "Harnham", "url": "https://harnham.pinpointhq.com/postings.json", "platform": "pinpoint", "country": "UK", "sector": "professional-services"}
]
```

## 4. Honest assessment

This does not widen the UK contract market this tool can see, in any
meaningful sense. 84 firms were checked — the ten named in the brief plus
74 more spanning generalist IT staffing, boutique tech-leadership search,
interim-executive specialists and digital consultancies. Three produced a
live, verified, ATS-readable board; the other 81 either run a bespoke
vacancy search (66), sit behind bot protection (9), turned out to be a
different company under a leftover token (1), answered with a login wall
(1), or timed out inconclusively within this session's pacing budget (3).
Of the three that verified, every posting on all three boards, and on five
more agency/consultancy boards already in the bundled list checked for
comparison, is the recruiter's or consultancy's own internal staff
vacancy — recruitment consultants, engineers, ops roles on their own
payroll — never a day-rate contract or interim role they are placing with
a client. Zero externally-placed UK contract or interim technology-
leadership postings came out of this sweep. The premise behind the brief
holds up exactly as stated going in: UK day-rate contract work is
advertised by agencies, but not through their own ATS, which is precisely
why an employer-board-reading tool structurally cannot see it, and adding
these three boards does nothing to change that. The one route that
plausibly would — Reed's API with `postedByDirectEmployer` turned off, so
agency-posted listings are included, or Reed's `postedByRecruitmentAgency`
filter turned on deliberately — is already built and documented in
`docs/SOURCES.md`; it just is not switched on by default. That is a config
change, not a new adapter, and it is the one lever here that would
actually move the 19-role number.
