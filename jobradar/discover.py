"""Find an employer's job board from their careers page.

This is the piece that stops the source list being a hand-curated artifact
that rots. Given a company domain or careers URL it follows the redirect
chain, reads the landing page for an embedded ATS, extracts the token, builds
the API URL and proves it by counting live postings.

It matters most for Workday, where tenant and site names cannot be guessed at
all. 117 attempts at guessing them produced zero working tenants; following
the careers-page redirect works first time.

Identity is checked, not assumed. A token that merely responds is not proof it
is the right company: Ashby `primer` is a Florida schools operator, not the
London payments company, and Greenhouse `peak` is a Texas physiotherapy chain.
So `verify` compares the board's own apply links and company name against the
domain you asked for and reports a mismatch rather than banking it.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, replace
from functools import lru_cache
from urllib.parse import urlparse, urlsplit

import requests

from . import adapters
from .models import Source

UA = "job-radar/0.1 (+https://github.com/maccydee/job-radar) source discovery"

CAREERS_PATHS = [
    "/careers", "/jobs", "/careers/jobs", "/about/careers", "/company/careers",
    "/en/careers", "/work-with-us", "/join-us", "/vacancies", "/careers/open-roles",
]

# Signatures found in the redirect target or the page body.
#
# Every repetition here is bounded, and nothing may reintroduce a bare `+` or
# `*`. An unbounded class in front of the vendor's hostname, as in the old
# `([a-z0-9-]+)\.taleo\.net/...`, is quadratic on any long run of characters
# the class accepts: the engine starts a fresh attempt at each of the 400,000
# offsets in the page and, at each one, swallows the whole run before the next
# literal fails. Under the `re.I` in `_scan` the class also accepts uppercase,
# so a minified bundle or a base64 blob is one single run. That cost 120
# seconds per page and found nothing, and it killed two large-cap discovery
# runs before `faulthandler` traced it.
#
# 63 is the DNS label limit (RFC 1035), so bounding a hostname label there
# cannot lose a real host. 80 for a path segment or a plain token is far above
# anything real: the longest live one is Tesco's two-segment Avature prefix,
# `en_GB/careersmarketplace`, at 24 characters.
SIGNATURES: list[tuple[str, str]] = [
    ("greenhouse", r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_.-]{1,80})"),
    ("greenhouse", r"greenhouse\.io/v1/boards/([a-z0-9_.-]{1,80})"),
    ("ashby", r"jobs\.ashbyhq\.com/([a-z0-9_.-]{1,80})"),
    ("ashby", r"ashbyhq\.com/posting-api/job-board/([a-z0-9_.-]{1,80})"),
    # Lever tokens are case-sensitive on the wire: api.eu.lever.co answers 200
    # for `Expana` and 404 for `expana`. The character class is spelled with
    # both cases so that stays true if the re.I in `_scan` is ever dropped, and
    # nothing here may lowercase the capture.
    ("lever", r"jobs\.lever\.co/([A-Za-z0-9_.-]{1,80})"),
    # Lever's EU deployment is a different host with different data, so it
    # needs its own signature. `jobs.lever.co/` cannot match `jobs.eu.lever.co/`
    # (the literal substring is not there), so the two never collide. Europe is
    # where the boards actually are: jobs.lever.co/robots.txt is Disallow:/ for
    # CCBot, ClaudeBot and GPTBot, jobs.eu.lever.co/robots.txt is Allow:/, so a
    # crawl-derived employer list finds EU boards and almost no US ones.
    ("lever_eu", r"jobs\.eu\.lever\.co/([A-Za-z0-9_.-]{1,80})"),
    ("workable", r"apply\.workable\.com/([a-z0-9_.-]{1,80})"),
    ("smartrecruiters", r"(?:jobs|careers)\.smartrecruiters\.com/([a-zA-Z0-9_.-]{1,80})"),
    ("recruitee", r"([a-z0-9-]{1,63})\.recruitee\.com"),
    ("breezy", r"([a-z0-9-]{1,63})\.breezy\.hr"),
    ("teamtailor", r"([a-z0-9-]{1,63})\.teamtailor\.com"),
    # Pinpoint also sells custom careers domains (careers.<employer>.com), and
    # a board on one of those is invisible to a hostname signature. Those have
    # to be added by hand; this finds the subdomain-hosted ones.
    ("pinpoint", r"([a-z0-9-]{1,63})\.pinpointhq\.com"),
    ("bamboohr", r"([a-z0-9-]{1,63})\.bamboohr\.com"),
    ("jazzhr", r"([a-z0-9-]{1,63})\.applytojob\.com"),
    # Jobvite is the odd one out: the token is a path segment, not a
    # subdomain, because every customer sits on the one jobs.jobvite.com host.
    ("jobvite", r"jobs\.jobvite\.com/([A-Za-z0-9_.-]{1,80})"),
    ("personio", r"([a-z0-9-]{1,63})\.jobs\.personio\.(?:de|com)"),
    # Oracle needs the whole host, not a short token, and the host bears no
    # relation to the company name.
    ("oracle", r"([a-z0-9-]{1,63}\.fa\.[a-z0-9]{1,63}\.oraclecloud\.com)"),
    # Avature and RMK are whole-host platforms with a path prefix on top, so
    # their token is composite: `host|prefix`, the same string
    # `adapters.build_avature` takes. They used to capture `host/prefix` in one
    # group, which nothing could build a URL from, so `_scan` dropped every hit
    # (it skips platforms with no `build`) and neither platform was ever found
    # by `discover` at all.
    ("avature", r"([a-z0-9-]{1,63}\.avature\.net)/([a-zA-Z0-9_-]{1,80})"),
    # The customer-hosted form, which is the one that matters. Avature serves
    # as often from the employer's own domain as from its own, and Tesco is on
    # careers.tesco.com: a host signature cannot see it, so the signature has
    # to be the path. Tesco's prefix is two segments (`en_GB/careersmarketplace`),
    # hence the optional second one.
    ("avature",
     r"(?:https?://)?([a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63}){1,6})"
     r"/([a-zA-Z0-9_-]{1,80}(?:/[a-zA-Z0-9_-]{1,80})?)/SearchJobs"),
    ("rmk", r"([a-z0-9-]{1,63}\.jobs2web\.com)/([a-zA-Z0-9_-]{1,80})"),
    # Taleo is composite for a different reason to Avature and RMK: the second
    # group is not a vendor path prefix, it is which of the tenant's career
    # sections this is, and a tenant runs several with no default among them.
    # Hilton's is `us_hotel_ext`, Transport for London's is `external`,
    # D.R. Horton's and TTEC's are both `2`. Guessing the section does not
    # fail loudly either: a section that does not exist answers 200 with
    # "Career Section Unavailable", which reads as an empty board.
    ("taleo", r"([a-z0-9-]{1,63})\.taleo\.net/careersection/([a-zA-Z0-9_-]{1,80})"),
    ("icims", r"([a-z0-9-]{1,63})\.icims\.com"),
]

# Workday needs two captures (tenant, site) and its own URL shape.
WORKDAY_RE = re.compile(
    r"https?://([a-z0-9-]{1,63})\.(wd\d{1,4})\.myworkdayjobs\.com"
    r"/(?:[a-z]{2}-[A-Z]{2}/)?([a-zA-Z0-9_-]{1,80})",
    re.I,
)

# Junk tokens come in two kinds, and applying one kind to the other cost a
# real board. The set used to be one list applied to group 1 of EVERY
# signature, but group 1 means different things on different platforms:
#
#   help.breezy.hr                       a vendor hostname. Never an employer.
#   boards-api.greenhouse.io/v1/boards/help/jobs
#                                        an employer slug. "Help" is a real
#                                        company in the bundled list, and once
#                                        "help" joined the shared set `_scan`
#                                        on job-boards.greenhouse.io/help
#                                        returned nothing at all.
#
# So the vendor-infrastructure words below are only ever tested against a
# capture that IS a hostname label. A path capture is the employer's own
# choice of slug and only the words that can never be one are excluded.
#
# Path words: these are structural pieces of the vendor's own URL grammar
# (`/embed/job_board?for=`, `/v1/boards/`, `/api/`, `?search=`), so a capture
# equal to one of them means the regex matched the scaffolding rather than a
# token. No employer slug can be one of these, because the vendor's own routing
# would shadow it.
_JUNK_PATH_TOKENS = {"embed", "job_board", "v1", "boards", "jobs", "api",
                     "search"}

# Hostname words: subdomains the vendor runs for itself. A careers page that
# embeds a Breezy board also links app.breezy.hr and help.breezy.hr, and each
# would otherwise be offered as a separate employer board to go and validate.
# "career", "careers", "partner" and "dashboard" are the same problem on
# Teamtailor: support.teamtailor.com and partner.teamtailor.com are the
# vendor's own, and career.teamtailor.com is Teamtailor recruiting for itself,
# which is a real board but never the board of the employer whose page we just
# read. "staticfe", "images4", "bhrpendo" and "resources" are four hosts a
# live BambooHR board page links alongside the employer's own.
#
# The cost of listing a word here is that an employer whose Breezy subdomain
# is literally that word has to be added by hand; the cost of not listing it
# is offering the vendor's own board as every customer's board. That trade is
# only acceptable on a hostname, which is why this set stops there.
_JUNK_HOST_LABELS = _JUNK_PATH_TOKENS | {
    "www", "app", "help", "support", "blog", "status", "docs",
    "career", "careers", "partner", "dashboard", "developers",
    "documentation", "resources",
    "staticfe", "images4", "bhrpendo",
    "static", "images", "img", "assets", "cdn", "scripts", "styles",
    "policy", "cookies", "privacy", "consent",
}

# Signatures whose first capture is a path segment the EMPLOYER chose, not a
# hostname label the vendor chose. These get `_JUNK_PATH_TOKENS` only, and are
# exempt from `_VENDOR_INFRA` below for the same reason: "policy", "stage" and
# "scripts" are all plausible company slugs, and none of them can be a vendor
# subdomain here because the host is fixed by the signature itself.
_PATH_CAPTURE_PLATFORMS = {"greenhouse", "ashby", "lever", "lever_eu",
                           "workable", "smartrecruiters", "jobvite"}

# `_JUNK_HOST_LABELS` matches whole tokens out of a set, which cannot express the
# shape that did the damage: vendor infrastructure hostnames that carry a
# deployment number or a purpose in the name, so no fixed list of words ever
# catches them.
#
#   rmk-map-12.jobs2web.com          SuccessFactors' shared job-map widget. It
#                                    is embedded by every RMK careers site that
#                                    draws a map.
#   cookie-policy-scripts.icims.com  iCIMS' cookie-banner host, picked up the
#                                    same way.
#
# Between them those two were extracted as the employer's own token for 40
# large-cap companies in one probe run, every one of them resolving to the
# same 404. The cost is not the dead row: it is that "this employer runs
# SuccessFactors and we cannot read it yet" turns into "we found their RMK
# board and it is empty", which is the wrong diagnosis and points the next
# adapter at the wrong platform.
#
# Matched with `fullmatch` against the first label of the host only, never as
# a substring. A real employer may well be called Mapfre, Scripts or
# Staticiel, and `discover` reporting "nothing found" for a live board is a
# worse outcome than one junk row a human deletes, so nothing here fires on a
# name that merely contains one of these words.
_VENDOR_INFRA = re.compile(
    r"rmk-map(?:-\d+)?|cookie-policy-scripts|stage|www\d+|static\d*|"
    r"assets\d*|cdn\d*|img\d*|images?\d*|scripts?|styles?|policy|cookies?|"
    r"privacy|consent",
    re.I)

# Platforms we can recognise but cannot read yet. Naming them turns fifteen
# identical shrugs into a diagnosis, and tells the maintainer which adapter to
# write next from real runs rather than guesswork. UK charity and public-sector
# recruitment runs almost entirely on these.
UNSUPPORTED = [
    ("Eploy", r"\beploy\b"),
    ("Hireserve", r"hireserve|/jobs/home/?(?=\s|[?#]|$)"),
    ("Jobtrain", r"jobtrain\.co\.uk|/Home/Job(?=\s|[?/#]|$)"),
    ("Networx", r"networxrecruitment\.com"),
    ("Oracle EBS iRecruitment", r"/OA_HTML/"),
    ("Oleeo", r"oleeo\.com|\.tal\.net"),
    # 589 employer hosts and not one of them readable. The careers page is an
    # empty SPA shell and the job data comes from `services/x/career-site/v1/`,
    # which answers 401 "no Authorization header found" to everything. The
    # page mints that token at runtime, so the only way in is lifting it back
    # out, which is not a published API and is not something this tool does.
    # Named here so an employer on it gets a diagnosis rather than a shrug.
    ("Cornerstone OnDemand", r"\.csod\.com"),
    # Taleo used to sit here. It has an adapter now, so it is in SIGNATURES
    # instead. The older, pre-faceted career sections still cannot be read,
    # but they are indistinguishable from the readable ones by URL shape, so
    # naming them here would mean labelling every readable Taleo board
    # unsupported. `fetch_taleo` reports the difference instead, by name, once
    # it has actually looked at the page.
    ("iCIMS portal", r"icims\.com/jobs/search(?!.*in_iframe)"),
    ("Workday (site unknown)", r"myworkdayjobs\.com(?!.*/wday/cxs)"),
    ("Civil Service Jobs", r"civilservicejobs\.service\.gov\.uk"),
    ("CharityJob", r"charityjob\.co\.uk"),
]


def detect_unsupported(text: str, url: str) -> str:
    blob = f"{url}\n{text[:200_000]}"
    for name, pat in UNSUPPORTED:
        if re.search(pat, blob, re.I):
            return name
    return ""


@dataclass
class Found:
    company: str
    url: str
    platform: str
    token: str
    domain: str | None = None
    live_jobs: int = 0
    # ok | mismatch | unchecked | unreadable | blocked | unsupported.
    # "unreadable" means we found the board and could not fetch it, which
    # is never a reason to drop it from the result or to call it dead.
    identity: str = "unchecked"
    note: str = ""

    def to_source(self) -> Source:
        return adapters.prepare(Source(
            company=self.company, url=self.url, platform=self.platform,
            domain=self.domain,
        ))


# Some large employers put their careers site behind bot protection. Tesco
# answers 403 from Akamai; Sainsbury's replies "You got banned permanently from
# this server". That is a clear no, and the honest thing is to report it as
# blocked rather than as "no job board found" and rather than working around
# it. Nothing in this tool tries to defeat bot protection.
_BLOCKED = re.compile(
    r"access denied|you got banned|permission to access|cloudflare|"
    r"captcha|are you a robot|bot detection|request blocked|akamai",
    re.I,
)


def _get(url: str, timeout: int = 12) -> requests.Response | None:
    try:
        return requests.get(url, timeout=timeout, allow_redirects=True,
                            headers={"User-Agent": UA,
                                     "Accept": "text/html,application/json"})
    except requests.RequestException:
        return None


def _is_blocked(r: requests.Response | None) -> bool:
    """Did this host actually refuse us?

    Substring-matching "cloudflare" or "captcha" over any response body marked
    three working charity sites as bot-protected: a Cloudflare-served 404 for
    a path that does not exist is not a block, and neither is a hidden captcha
    field on a job-alert signup form inside a perfectly good 200.
    """
    if r is None:
        return False
    if r.status_code in (401, 403, 429):
        return True
    # A page that served us content did not refuse us, whatever words are in it.
    if r.status_code < 400:
        return False
    return bool(_BLOCKED.search(r.text[:2000])) if r.text else False


def _candidates(target: str) -> list[str]:
    """Careers URLs to try, in rough order of likelihood.

    Large employers rarely put the ATS on `<domain>/careers`. They use a
    careers subdomain, or a separate careers domain entirely, so those are
    tried too.
    """
    t = target.strip().rstrip("/")
    if t.startswith("http"):
        return [t]

    if "." in t and " " not in t:
        host = t.lstrip("/")
        root = host.replace("www.", "")
        bare = root.split(".")[0]
        tld = ".".join(root.split(".")[1:]) or "com"
        bases = [
            f"https://{root}",
            # www is not redundant. Plenty of employers serve nothing on the
            # apex domain, so dropping it loses them outright: the Bank of
            # England answers on www and not without it.
            f"https://www.{root}",
            f"https://careers.{root}",
            f"https://jobs.{root}",
            f"https://{bare}.careers",
            f"https://careers.{bare}.{tld}",
        ]
    else:
        slug = re.sub(r"[^a-z0-9]", "", t.lower())
        bases = [f"https://{slug}.com", f"https://careers.{slug}.com",
                 f"https://jobs.{slug}.com"]

    out: list[str] = []
    for b in bases:
        out.append(b)
        out.extend(b + p for p in CAREERS_PATHS)

    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq[:40]


_ESCAPE = re.compile(r"\\.")
_CHAR_CLASS = re.compile(r"\[[^\]]*\]")
_REPEAT = re.compile(r"\{\d+(?:,\d*)?\}")
_LITERAL_RUN = re.compile(r"[A-Za-z0-9]{4,}")


def _blank(m: re.Match) -> str:
    """Same-length spaces, so offsets into the masked pattern still
    line up with the pattern itself."""
    return " " * len(m.group(0))


# The longest string any signature above can match is the Avature path form:
# seven 63-character labels, two 80-character path segments, the scheme and
# the separators, which is under 700. The window margin has to be at least
# that, or a match could straddle a window edge and be lost. 1024 leaves room
# for a signature that grows without anyone re-doing this arithmetic, and
# `test_no_signature_can_repeat_without_a_bound` fails if one ever exceeds it.
_MARGIN = 1024


@lru_cache(maxsize=256)
def _required_literals(pat: str) -> tuple[str, ...]:
    """Literal words a page MUST contain for this signature to match at all.

    Required, not merely present: a word is only returned if it sits outside
    every optional group and every alternation, so skipping a page that lacks
    one cannot skip a page that would have matched. `https` in the Avature
    path signature is inside `(?:https?://)?` and is dropped for that reason;
    `boards` in the Greenhouse signature is inside `(?:boards|job-boards)` and
    is dropped for the other.

    Derived from the pattern rather than listed beside it, because a hand
    table silently stops covering whatever gets added next. Taleo is the
    example: it was added to `SIGNATURES` long after the scanner was written.
    """
    # Mask escapes first so `\[` never reads as a class opener and `\.` never
    # reads as a literal, then mask classes. Both substitutions preserve
    # length, so offsets still line up with the original pattern.
    masked = _ESCAPE.sub("  ", pat)
    masked = _CHAR_CLASS.sub(_blank, masked)
    # The digits in `{1,63}` are not a literal the page has to contain. Left
    # in, a bound like `{1,1000}` would make "1000" a required word and every
    # page without it would be skipped, which is the silent way to lose a
    # platform.
    masked = _REPEAT.sub(_blank, masked)

    stack: list[list] = []
    groups: list[tuple[int, int, bool]] = []   # (open, close, skip-what's-inside)
    for i, ch in enumerate(masked):
        if ch == "(":
            stack.append([i, False])
        elif ch == "|":
            if not stack:
                return ()      # a top-level alternation: no word is required
            stack[-1][1] = True
        elif ch == ")" and stack:
            start, alternation = stack.pop()
            after = masked[i + 1] if i + 1 < len(masked) else ""
            optional = after in ("?", "*")
            groups.append((start, i, alternation or optional))

    out: list[str] = []
    for m in _LITERAL_RUN.finditer(masked):
        s, e = m.span()
        if (masked[e] if e < len(masked) else "") in ("?", "*"):
            continue           # the last character of the run is optional
        if any(gs < s and e <= ge and skip for gs, ge, skip in groups):
            continue
        out.append(m.group(0).lower())
    return tuple(dict.fromkeys(out))


@lru_cache(maxsize=256)
def _compiled(pat: str) -> re.Pattern:
    return re.compile(pat, re.I)


def _scan_signature(pat: str, blob: str, low: str):
    """Yield this signature's matches without letting it loose on the whole page.

    Two pages killed a large-cap run at 120 seconds each. Bounding the
    repetitions in `SIGNATURES` takes that from quadratic to linear, and this
    takes the linear pass off all but a few hundred bytes of the page: a
    signature whose required words are absent is not run at all, and one whose
    words are present is run only on windows around them. A 400KB careers page
    is mostly minified JavaScript that no signature can match, and skipping it
    with `str.find` is a C-speed substring search rather than a regex walk.

    The match objects are relative to their window, so their `.span()` means
    nothing in the page. Only `.group()` may be used on them.
    """
    rx = _compiled(pat)
    lits = _required_literals(pat)
    if not lits:
        # No word is required, so nothing can be ruled out. Still bounded, so
        # still linear; this is the safe fallback, not the fast path.
        yield from rx.finditer(blob)
        return
    if not all(w in low for w in lits):
        return

    anchor = max(lits, key=len)
    spans: list[list[int]] = []
    at = 0
    while True:
        i = low.find(anchor, at)
        if i < 0:
            break
        lo, hi = max(0, i - _MARGIN), i + len(anchor) + _MARGIN
        if spans and lo <= spans[-1][1]:
            spans[-1][1] = hi          # merge, so a dense page is scanned once
        else:
            spans.append([lo, hi])
        at = i + len(anchor)
    for lo, hi in spans:
        yield from rx.finditer(blob[lo:hi])


def _scan(text: str, final_url: str) -> list[tuple[str, str, str]]:
    """Returns [(platform, token, api_url)] found in a page or its redirect."""
    blob = f"{final_url}\n{text[:400_000]}"
    low = blob.lower()
    hits: list[tuple[str, str, str]] = []

    for m in WORKDAY_RE.finditer(blob):
        tenant, wd, site = m.group(1), m.group(2), m.group(3)
        if site.lower() in ("login", "home"):
            continue
        api = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
        hits.append(("workday", f"{tenant}/{site}", api))

    # Phenom does not expose a token at all; the careers host itself is the
    # source, and it is recognisable from the assets it loads. The URL is built
    # by the adapter rather than spelled out here, because it was spelled out
    # in three places and `enumerate_boards` had a fourth copy of the same idea
    # for Workday that had already drifted.
    if re.search(r"phenompeople|phApp\.ddo", blob, re.I):
        host = urlparse(final_url).netloc
        if host:
            ph = adapters.by_name("phenom")
            hits.append(("phenom", host, ph.build(host)))

    for platform, pat in SIGNATURES:
        for m in _scan_signature(pat, blob, low):
            # A signature with more than one group is a composite token, joined
            # with "|" exactly as `adapters.build_avature` and the harvester's
            # columnar extractor spell it. The junk test stays on the FIRST
            # group only: the rest are path segments in the vendor's namespace,
            # and `_JUNK_TOKENS` contains "careers", which is what Tesco Bank's
            # Avature site is actually called.
            tok = "|".join(g or "" for g in m.groups())
            head = m.group(1) or ""
            # Which set applies depends on what group 1 actually is. On
            # Greenhouse it is the employer's own slug, so the vendor's
            # subdomain words must not be tested against it: "help" is a real
            # company in the bundled list and testing it as a hostname label
            # made its live board undiscoverable.
            path_capture = platform in _PATH_CAPTURE_PLATFORMS
            junk = _JUNK_PATH_TOKENS if path_capture else _JUNK_HOST_LABELS
            if not head or head.lower() in junk or len(head) < 2:
                continue
            # Whole-host platforms (Oracle, Avature, RMK) capture the host in
            # the first group, subdomain platforms capture just the label, so
            # the vendor test has to run on the first label either way:
            # `rmk-map-12.jobs2web.com` and `cookie-policy-scripts` are the
            # same fault wearing two shapes. It is skipped for path captures,
            # where the first "label" is an employer slug and Stagecoach or
            # Policy Expert would be thrown away by it.
            if not path_capture and _VENDOR_INFRA.fullmatch(head.split(".")[0]):
                continue
            p = adapters.by_name(platform)
            if not p or not p.build:
                continue
            hits.append((platform, tok, p.build(tok)))

    seen, out = set(), []
    for h in hits:
        if h[2] in seen:
            continue
        seen.add(h[2])
        out.append(h)
    return out


# Platforms that cannot answer at all without a credential. `validate` carries
# none, so a failure from one of these is a fact about `validate`, not about
# the source, and `validate --prune` deletes what looks dead.
KEYED_PLATFORMS = {"reed", "adzuna"}


def _parse_or_why(payload, src: Source) -> tuple[list, str | None]:
    """Parse a fetched payload, or say why it could not be parsed.

    `adapters.parse` catches every exception and returns `[]`, which is right
    for a scan -- one malformed board must not end a run over 13,000 sources
    -- and is exactly wrong here, because here the zero is a verdict. A board
    answering HTTP 200 with `<html>Service Unavailable</html>` instead of its
    usual JSON raises inside the platform's parser, and `count_jobs` reported
    `(0, [], None)`: no error, no postings. `validate_source` turns that into
    "dead / no postings returned / prunable: True", and the weekly maintenance
    workflow runs `validate --prune` unattended, so a live employer is deleted
    from the source list because their board was briefly serving a holding
    page. That is the 429-as-an-empty-board fault (9d74c68) arriving through
    the parser instead of through HTTP.

    So the parse happens here, where the exception can be turned into the
    third value that already means "we could not read it".
    """
    p = adapters.by_name(src.platform) or adapters.detect(src.url)
    try:
        return [j for j in p.parse(payload, src) if j.title and j.url], None
    except Exception as e:
        return [], (f"the board answered, but its {src.platform or 'response'} "
                    f"could not be read ({type(e).__name__}: "
                    f"{str(e)[:120]}). That is not the same as having no "
                    f"postings, so it is not a dead board")


def count_jobs(src: Source, timeout: int = 25,
               *, transport: list | None = None) -> tuple[int, list, str | None]:
    """Fetch and parse. Job count is the only reliable liveness signal:
    several of these platforms answer 200 with an empty array for tokens that
    do not exist, so status codes prove nothing.

    The third value separates "this board has no postings" from "we could not
    read it". They used to be the same answer, and the difference matters
    every time: a 429 from a busy platform was being reported as a dead board,
    and `validate --prune` would then delete a real employer. Callers that
    only want the count can ignore it, but nothing may treat it as zero.

    `transport` is an out-parameter: pass a list and the name of the TLS alert
    is appended to it when the request never got as far as HTTP. Callers that
    delete things need that as a flag rather than as prose in the third value.
    """
    from . import fetch as fetch_mod
    from .fetch import fetch_one
    # A platform with its own fetcher has one because a plain GET of its board
    # URL does not return the postings. `fetch_one` on such a source reports
    # zero jobs, `validate` calls that dead, and `validate --prune` deletes a
    # live employer.
    #
    # Taleo was special-cased here for exactly that reason, and only Taleo,
    # which made this a list of one that nobody extended. On 7 September 2026
    # a validate run marked BOTH Google Careers boards dead and prunable while
    # they were serving 120 UK and 1,855 US roles: `fetch_google_careers`
    # reads the postings out of an `AF_initDataCallback` block in the page,
    # and a plain GET returns that page with nothing a parser can see. The
    # adapter had been added four days earlier and the weekly job was already
    # queued to delete it.
    #
    # So the rule is the fetcher, not the name. One page is enough to answer
    # "is this board alive", and the signatures differ, so the call is built
    # per platform rather than guessed at.
    special = getattr(fetch_mod, f"fetch_{src.platform}", None)
    if special is not None and src.platform not in KEYED_PLATFORMS:
        kw = {"timeout": timeout, "retries": 1, "user_agent": UA,
              "max_pages": 1}
        try:
            params = inspect.signature(special).parameters
        except (TypeError, ValueError):
            params = {}
        try:
            if "terms" in params:
                res = special(src, [], **{k: v for k, v in kw.items()
                                          if k in params})
            else:
                res = special(src, **{k: v for k, v in kw.items()
                                      if k in params})
        except Exception as exc:
            # A bespoke fetcher that throws is a thing this could not read,
            # never a board with nothing. `--prune` deletes on "dead".
            return 0, [], f"could not be read: {type(exc).__name__}: {exc}"[:200]
    else:
        res = fetch_one(src, timeout=timeout, retries=1, user_agent=UA)
    if not res.ok:
        why = res.error or (f"HTTP {res.status}" if res.status else "no answer")
        if res.status in (429, 503) or res.throttled:
            why = f"rate limited ({why})"
        elif res.status in (400, 401, 403) and src.platform in KEYED_PLATFORMS:
            # `validate` does not carry credentials, so it cannot speak to
            # these at all. Reporting a bare "HTTP 401" here reads as a broken
            # source and would have anyone with a perfectly good key in their
            # config hunting a fault that is not there. Adzuna is the reason
            # 400 is in that list as well as 401: an unkeyed Adzuna request is
            # a 400 with an HTML error page, not a 401.
            why = (f"needs an API key, which `validate` does not send; "
                   f"this says nothing about whether {src.platform.title()} "
                   f"is working")
        if res.transport and transport is not None:
            # Below HTTP. `fetch_one` already wrote the whole explanation into
            # `res.error`; what the caller needs from here is the machine-
            # readable fact, so a prune can refuse on it.
            transport.append(res.transport)
        return 0, [], why
    jobs, unreadable = _parse_or_why(res.payload, src)
    if unreadable:
        return 0, [], unreadable
    return len(jobs), jobs, None


def _norm(s: str) -> str:
    """Lowercase alphanumerics only, so 'Checkout.com' and 'checkout.com' and
    "Sotheby's" and 'sothebys' compare equal."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# Host labels that describe the careers SITE rather than the employer running
# it. `careers.monzo.com` is Monzo's careers site, not a company called
# Careers, and pasting that URL is the single most natural way to use this.
_SITE_LABELS = {
    "www", "www2", "www3", "careers", "career", "jobs", "job", "apply",
    "recruitment", "recruiting", "recruit", "hiring", "hire", "work",
    "workwithus", "talent", "people", "join", "joinus", "vacancies",
    "opportunities", "emea", "global",
}


def clean_host(value: str | None) -> str:
    """A bare lowercase hostname: no scheme, no credentials, no port, no path.

    `urlparse().netloc` keeps the userinfo and the port, and both were being
    written straight into the source list. `discover
    https://alice:s3cret@monzo.com/careers` stored `domain:
    alice:s3cret@monzo.com` and named the employer "Alice:S3Cret@Monzo": a
    password copied into a config file the user then has no reason to look at.
    `urlsplit().hostname` is the parsed form with all of that stripped.
    """
    v = (value or "").strip()
    if not v:
        return ""
    if "://" not in v:
        v = "//" + v.lstrip("/")
    try:
        return (urlsplit(v).hostname or "").lower()
    except ValueError:
        return ""


def employer_label(host: str | None) -> str:
    """The employer's own label out of a hostname.

    `netloc.split(".")[0]` was doing this job, and it is only right when the
    board sits on the apex domain. Every other shape gave the vendor's or the
    site's word instead of the employer's, and the answer was used for two
    things at once, so a wrong one cost twice:

      * as the company name, so `--add www.monzo.com` wrote a source called
        "Www" and every role it found was attributed to Www;
      * as the identity root, so `discover careers.monzo.com` compared the
        board's own name "Monzo" against "Careers", called that a MISMATCH,
        and `--add` then refused a perfectly good board.

    Stops stripping at two labels, so `jobs.com` stays "jobs" rather than
    becoming "com": a site label is only a site label when there is a
    registrable name left behind it.
    """
    labels = [p for p in clean_host(host).split(".") if p]
    while len(labels) > 2 and labels[0] in _SITE_LABELS:
        labels.pop(0)
    return labels[0] if labels else ""


def verify_identity(jobs: list, domain: str | None, company: str,
                    platform: str = "") -> tuple[str, str]:
    """Does this board actually belong to who we think?

    Compares the board's apply links and its own company name against the
    domain asked for. Returns (verdict, note). Being unable to tell is
    reported as `unchecked`, never as `ok`, because a confident wrong answer
    here is how a Florida schools operator gets filed as a payments company.
    """
    # Aggregated search endpoints return many employers by design, so there is
    # no single identity to check against.
    if platform == "linkedin":
        return "unchecked", "aggregated search results, many employers"

    if not jobs:
        return "unchecked", "no postings to check against"
    apply_hosts = {urlparse(j.url).netloc.lower() for j in jobs if j.url}
    names = {(j.company or "").strip() for j in jobs if j.company}
    norm_names = {_norm(n) for n in names if n}

    if domain:
        d = clean_host(domain) or domain.lower()
        # The employer's label, not the first one. On `careers.monzo.com` the
        # first label is "careers", which matches nothing a board ever calls
        # itself, so every careers-subdomain URL fell through to the `want`
        # test below and came back "mismatch" against a board that was right.
        root = _norm(employer_label(d))
        if root and any(root in _norm(h) for h in apply_hosts):
            return "ok", f"apply links point at {d}"
        if root and any(root in n or n in root for n in norm_names if n):
            return "ok", f"board names itself {sorted(names)[0]!r}"

    want = _norm(company)
    if want and any(want in n or n in want for n in norm_names if n):
        return "ok", f"board names itself {sorted(names)[0]!r}"
    if names:
        return "mismatch", f"board names itself {sorted(names)[0]!r}, expected {company!r}"
    return "unchecked", "board publishes no company name"


def _is_absent(err: str) -> bool:
    """Whether an error means "there is nothing at this address".

    Only 404 and 410. A 403 is a door that exists and is shut, a 429 is a
    door that exists and is busy, and a timeout is no answer at all: none of
    those is evidence of absence, and treating them as such is how a board
    that is merely blocked gets deleted.
    """
    t = (err or "").lower()
    return "404" in t or "410" in t or "not found" in t


def _guess_tokens(domain: str | None, target: str) -> list[tuple[str, str, str]]:
    """Obvious token spellings, built into API URLs for the platforms that
    accept a plain token. Every result is still verified by the caller.
    """
    base = (domain or target).lower().replace("www.", "")
    root = base.split(".")[0]
    tld_form = base if "." in base else ""
    candidates = {root, root.replace("-", ""), root.replace("-", "")}
    if tld_form:
        candidates.add(tld_form)          # Primer's real Ashby token is "primer.io"
        candidates.add(tld_form.replace(".", ""))
    candidates = {c for c in candidates if c and len(c) > 1}

    out: list[tuple[str, str, str]] = []
    for name in ("greenhouse", "ashby", "lever"):
        p = adapters.by_name(name)
        if not p or not p.build:
            continue
        for tok in sorted(candidates):
            out.append((name, tok, p.build(tok)))
    return out


def discover(target: str, company: str | None = None, *, validate: bool = True) -> list[Found]:
    """Resolve a company or careers URL to job boards."""
    found: list[Found] = []
    domain = None
    if target.startswith("http") or "." in target:
        # `hostname`, not `netloc`: the latter carries any user:password@ and
        # :port the person pasted, and this value is written into the config.
        domain = clean_host(target) or None
    name = company or (employer_label(domain) if domain
                       else target).replace("-", " ").title()

    # If the thing being asked about IS one of the platforms we cannot read,
    # say so and stop. Guessing tokens from the domain label answered
    # `civilservicejobs.service.gov.uk` with a real Greenhouse board named
    # "Civil Service Jobs" (it is one department's, holding a single posting),
    # marked it verified, and left a user believing the whole civil service
    # was covered. A platform domain is never an employer.
    #
    # Unless the target already names a board we can read, which is not the
    # same thing at all. `UNSUPPORTED` holds "Workday (site unknown)", so
    # pasting a live Workday board URL -- the exact case this module says it
    # exists for, because tenant and site cannot be guessed and the URL is the
    # only place they are written down -- was answered with "job-radar cannot
    # read it yet", without one request being made, while `WORKDAY_RE` sitting
    # in the same file reads the tenant and the site straight out of it. Same
    # for the readable iCIMS form. `_scan` on the target settles it: a hit
    # means there is a board here to try, and `count_jobs` will report
    # honestly if it turns out we cannot read it after all.
    on_target = _scan("", target)
    platform_hit = "" if on_target else detect_unsupported("", target)
    if platform_hit:
        return [Found(company=name, url=target if target.startswith("http")
                      else f"https://{target}", platform="", token="",
                      domain=domain, identity="unsupported",
                      note=f"{platform_hit} is a job platform, not an employer, "
                           f"and job-radar cannot read it yet. Searching it by "
                           f"hand is the only option for now.")]

    # Candidates are independent, so fetch them together rather than walking a
    # list of 30 URLs at 10 seconds each. First page that reveals an ATS wins.
    from concurrent.futures import ThreadPoolExecutor, as_completed

    cands = _candidates(target)
    # A URL that already names a board needs no careers page read to find it.
    hits: list[tuple[str, str, str]] = list(on_target)
    blocked = 0
    unsupported = ""          # a platform we recognised but cannot read yet
    unsupported_url = ""
    # Nothing to read a careers page for when the URL itself named the board.
    # Not merely a saving: it is up to 40 requests at a host that already told
    # us the answer.
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {} if hits else {ex.submit(_get, c, 10): c for c in cands}
        try:
            for fut in as_completed(futs, timeout=75):
                r = fut.result()
                if _is_blocked(r):
                    blocked += 1
                    continue
                if r is None or r.status_code >= 400:
                    continue
                got = _scan(r.text, r.url)
                if got:
                    hits.extend(got)
                    break
                if not unsupported:
                    plat = detect_unsupported(r.text, r.url)
                    if plat:
                        unsupported, unsupported_url = plat, r.url
        except TimeoutError:
            pass
        for f2 in futs:
            f2.cancel()

    # If the careers page could not be read, fall back to trying the obvious
    # tokens directly. Guessing used to be unsafe, because a token that
    # responds is not proof of the right company. It is safe here only because
    # every hit is identity-checked before it is offered, so the Florida
    # schools operator sitting on Ashby `primer` gets caught rather than filed.
    # It does not work for Workday at all: tenant and site names are not
    # derivable from a company name, and 117 attempts proved it.
    guessed: set[str] = set()
    if not hits:
        tokens = _guess_tokens(domain, target)
        guessed = {api for _p, _t, api in tokens}
        hits.extend(tokens)

    seen_api, checked = set(), []
    for platform, token, api in hits:
        if api in seen_api:
            continue
        seen_api.add(api)
        f = Found(company=name, url=api, platform=platform, token=token, domain=domain)
        if validate:
            n, jobs, err = count_jobs(f.to_source())
            f.live_jobs = n
            if err:
                # "We could not read this board" is not "this board does not
                # exist", and the whole point of keeping the two apart (9d74c68)
                # was lost one line further down, where the `live_jobs > 0`
                # filter dropped it and the caller then printed "nothing found
                # ... either it is rendered by JavaScript, or the platform has
                # no adapter yet". Every clause of that is false about a board
                # we located and then failed to fetch. So it is marked and
                # kept; `cmd_discover` prints it and refuses to `--add` it.
                # A 404 on a slug WE invented is not a board we failed to
                # read. It is the expected answer to a wrong guess, and there
                # is nothing there to report.
                #
                # `discover mollie.com` found the real board, an Ashby one
                # with 47 roles, and then printed seven blocks of "found, but
                # could not be read: HTTP 404 ... not added; try again later".
                # Every one of those was a token this tool made up, and "try
                # again later" will never work: there is no board at that
                # address and there never was.
                #
                # Deliberately scoped to guessed candidates only. `count_jobs`
                # is shared with `validate --prune`, and teaching THAT to read
                # 404 as dead is how a maintenance job opens a pull request
                # deleting 17,171 of 17,810 sources.
                if api in guessed and _is_absent(err):
                    continue
                f.identity = "unreadable"
                f.note = f"could not be read: {err}"
            elif n == 0:
                f.note = "responded but published no postings"
            else:
                f.identity, f.note = verify_identity(jobs, domain, name, f.platform)
        checked.append(f)

    # Empty boards are noise when they came from guessing rather than from a
    # link the company actually published. A board that errored is not empty:
    # its count is zero because the fetch failed, so it is kept and flagged.
    #
    # A board named in the URL the person typed is the opposite of a guess, so
    # it is kept at zero too. Dropping it answered a pasted board URL with
    # "nothing found ... either it is rendered by JavaScript, or the platform
    # has no adapter yet", about a board read straight out of the URL in front
    # of them, which is the same false statement 9d74c68 removed for the
    # unreadable case.
    named = {api for _p, _t, api in on_target}
    found = [f for f in checked
             if f.live_jobs > 0 or f.identity == "unreadable"
             or f.url in named or not validate]

    if not found:
        if unsupported:
            # Naming the platform turns an identical shrug into a diagnosis,
            # and tells the maintainer which adapter to write next from real
            # runs rather than guesswork.
            return [Found(company=name, url=unsupported_url, platform="",
                          token="", domain=domain, identity="unsupported",
                          note=f"{unsupported}, which job-radar cannot read yet. "
                               f"The board is at {unsupported_url} and is worth "
                               f"a bookmark. Adapter requests welcome.")]
        if blocked:
            return [Found(company=name, url="", platform="", token="", domain=domain,
                          identity="blocked",
                          note=f"careers site refused automated requests "
                               f"({blocked} of {len(cands)} URLs blocked), and no "
                               f"job board was found by name. Apply through their "
                               f"site directly.")]
        return []

    found.sort(key=lambda f: (-f.live_jobs, f.identity != "ok"))
    return found


# A neutral word to probe a keyword search with. It has to return something
# on any real job board without being specific to one field.
PROBE_KEYWORD = "manager"


def _count_with_transport(src: Source, alerts: list):
    """`count_jobs`, collecting the TLS alert when the callee accepts one.

    The suite replaces `count_jobs` with two-argument stand-ins to simulate a
    429 or an empty board, and a health check is the last thing that should
    break because somebody stubbed its dependency. Only a TypeError that names
    this exact parameter is swallowed; anything else is a real fault and is
    re-raised.
    """
    try:
        return count_jobs(src, transport=alerts)
    except TypeError as e:
        if "transport" not in str(e):
            raise
        return count_jobs(src)


def validate_source(src: Source) -> dict:
    """Health check for one already-known source. Used by `validate` and by
    the weekly maintenance workflow.

    Keyword searches (NHS Jobs, LinkedIn) ship as templates and are expanded
    per title at scan time. Fetching the unexpanded URL asks the board for
    postings matching the literal string "{keyword}", which returns nothing,
    which read as dead: the weekly prune was scheduled to delete NHS Jobs, a
    live source and the only direct one that works outside technology. So
    probe them with a real word instead, and skip the identity check, which
    asks "is this board really this employer's" and is meaningless for an
    aggregator that returns every employer.
    """
    if src.keyword_template or "{keyword}" in src.url:
        from urllib.parse import quote_plus
        from .sources import UnusableSourceURL, fill_template
        # `fill_template`, not `str.format`, and this is the whole reason that
        # function exists. Filling only `{keyword}` raises KeyError on any
        # other placeholder, the error comes back out of
        # `ThreadPoolExecutor.map` in `cmd_validate`, and the run dies at the
        # first source carrying one. Every source after it then goes unchecked
        # and the health check reports nothing about them either way.
        #
        # Not hypothetical, and not new: `sources.fill_template` was written
        # against exactly this, with a docstring saying one source carrying
        # `&loc={location}` killed a whole run. This call site was never moved
        # over, so the weekly validation has been failing on `Workable search`
        # (`location={country}`) with `KeyError: 'country'`, filing an issue
        # each week that blamed a throttled runner.
        #
        # `{country}` is filled with nothing here on purpose. This is a
        # liveness probe, and an aggregator asked for one keyword and no
        # location answers for everywhere, which is the broadest question and
        # the one least likely to read as dead.
        try:
            probe_url = fill_template(src.url, keyword=quote_plus(PROBE_KEYWORD))
        except UnusableSourceURL as e:
            # One unusable URL is a fact about one source. Report it and let
            # the other 17,922 be checked.
            return {
                "company": src.company,
                "url": src.url,
                "platform": src.platform,
                "live_jobs": 0,
                "verdict": "unreachable",
                "transport": None,
                # Never prunable. The tool could not ask the question, which
                # is not the same as the board having no answer, and this
                # verdict is what `--prune` deletes on.
                "prunable": False,
                "note": f"could not be probed: {e}",
            }
        probe = replace(src, url=probe_url, keyword_template=False)
        alerts: list = []
        n, _, err = _count_with_transport(probe, alerts)
        return {
            "company": src.company,
            "url": src.url,
            "platform": src.platform,
            "live_jobs": n,
            "verdict": "unreachable" if err else ("dead" if n == 0 else "live"),
            "transport": alerts[0] if alerts else None,
            # An ALREADY EXPANDED search is never prunable, however empty.
            #
            # `_load_sources` expands one template into a search per title and
            # per country, so `validate` sees rows like "Workable search: vice
            # president engineering in United Arab Emirates". That returning
            # nothing today is not a dead source, it is a query with no
            # current results, and tomorrow it may have some. There is also
            # nothing to delete: the expansion is not a row in sources.json,
            # only the template is.
            #
            # It still did damage. On 7 September 2026 six such searches were
            # counted among the dead, and the dead count is what the weekly
            # prune measures against its cap of 250. Transient empty searches
            # inflating that number is how a legitimate prune gets refused,
            # which leaves the list unvalidated, which makes next week's
            # number larger still.
            #
            # An UNEXPANDED template is a different question and stays
            # prunable: it is probed with a real word, and if the board
            # answers nothing to that it may really be gone.
            "prunable": (not err and n == 0 and not alerts
                         and "{keyword}" in src.url),
            "note": f"could not be read: {err}" if err else
                    ("keyword search, probed with "
                     f"'{PROBE_KEYWORD}'; identity not checked"
                     + ("" if "{keyword}" in src.url else
                        "; an expanded search, so never pruned")),
        }

    alerts: list = []
    n, jobs, err = _count_with_transport(src, alerts)
    if err:
        # Not a verdict on the board. Something between here and it failed,
        # and calling that dead is how a live employer gets pruned.
        verdict, note = "unreachable", f"could not be read: {err}"
    elif n == 0:
        verdict, note = "dead", "no postings returned"
    else:
        verdict, note = verify_identity(jobs, src.domain, src.company,
                                        src.platform)
    if alerts:
        # Belt and braces, and the braces are the point. A TLS alert cannot
        # reach the `n == 0` branch today because `count_jobs` returns a
        # reason with it, but "dead" here is what `--prune` deletes on, and
        # the cost of one future refactor dropping that reason is a live
        # employer removed from the shipped list. Pin it.
        verdict = "unreachable"
    return {
        "company": src.company,
        "url": src.url,
        "platform": src.platform,
        "live_jobs": n,
        "verdict": verdict,
        # The TLS alert name when the handshake never completed, else None.
        "transport": alerts[0] if alerts else None,
        "prunable": prunable_row_verdict(verdict, alerts),
        "note": note,
    }


def prunable_row_verdict(verdict: str, alerts: list) -> bool:
    """May a row with this verdict be deleted from a source list?

    Only "dead" means the board answered and had nothing, and only a row that
    reached HTTP at all can be "dead". A handshake failure is a fact about the
    machine running `validate`, so it is never prunable however many Sundays
    in a row it reports.
    """
    return verdict == "dead" and not alerts


def prunable(row: dict) -> bool:
    """Whether `validate --prune` may delete the source this row describes.

    Rows from older reports have no `prunable` key; fall back to the same rule
    rather than defaulting to True, because the default here is a deletion.
    """
    if "prunable" in row:
        return bool(row["prunable"])
    return prunable_row_verdict(row.get("verdict", ""),
                                [row["transport"]] if row.get("transport") else [])
