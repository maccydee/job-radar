"""Config loading. Everything the user tunes lives in one YAML file."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# The fetcher owns these: it is what has to live with them.
from .fetch import DEFAULT_CONCURRENCY, MAX_CONCURRENCY
# The parser owns which currencies exist. `VALID_CURRENCIES` is built from it
# below so a floor can be written in anything a salary can come back as.
from .salary import KNOWN_CURRENCIES

# config.local.yaml wins if present. That is how you keep your own settings off
# a public fork: config.yaml is committed so GitHub Actions can read it, and
# config.local.yaml is gitignored for anything you would rather not publish.
SEARCH_PATH = [Path("config.local.yaml"), Path("config.yaml")]
DEFAULT_PATH = SEARCH_PATH[-1]


def resolve(path=None) -> Path:
    if path:
        return Path(path)
    env = os.environ.get("JOB_RADAR_CONFIG")
    if env:
        return Path(env)
    for p in SEARCH_PATH:
        if p.exists():
            return p
    return DEFAULT_PATH


@dataclass
class Dealbreaker:
    name: str
    pattern: str
    hard: bool = True

    def compiled(self):
        return re.compile(self.pattern, re.I)


@dataclass
class Config:
    titles_include: list[str] = field(default_factory=list)
    titles_exclude: list[str] = field(default_factory=list)

    countries: list[str] = field(default_factory=list)
    remote_ok: bool = True
    # Which working arrangements to keep. Empty means all of them, which is
    # the old behaviour and the default.
    #
    # `remote_ok` is a boolean answering a three-way question: True shows
    # remote AND everything else, False hides remote, and nothing said "hide
    # anything that is not remote". A remote-only reader had no way to express
    # the one thing their whole search is about, and 30 of their 40 matches
    # were office and hybrid roles in cities they will not move to.
    work_modes: list[str] = field(default_factory=list)
    relocate_to: list[str] = field(default_factory=list)
    # Countries where you would need a visa. A role there that says outright
    # it will not sponsor is one you cannot take, however good the fit.
    need_sponsorship: list[str] = field(default_factory=list)
    exclude_locations: list[str] = field(default_factory=list)

    salary_floor: float | None = None
    salary_currency: str = "GBP"

    dealbreakers: list[Dealbreaker] = field(default_factory=list)

    sectors: list[str] = field(default_factory=list)
    source_countries: list[str] = field(default_factory=list)
    use_bundled_sources: bool = True
    extra_sources: list[dict] = field(default_factory=list)
    # Reed's jobseeker API is the one source here that needs a credential.
    # Empty means the Reed source is skipped with a message rather than
    # fetched into a 401. See `_api_key` for where it can come from.
    reed_api_key: str = ""
    # Adzuna needs two, and both travel in the query string because Adzuna
    # offers no header authentication. Same rule as Reed: empty means the
    # source is skipped with a message rather than fetched into a 400.
    adzuna_app_id: str = ""
    adzuna_app_key: str = ""

    cv_path: str = ""
    formats: list[str] = field(default_factory=lambda: ["html", "json"])
    out_dir: Path = Path("out")

    concurrency: int = DEFAULT_CONCURRENCY
    timeout: int = 20
    retries: int = 2
    user_agent: str = (
        "job-radar/0.1 (+https://github.com/maccydee/job-radar) "
        "personal job search tool"
    )

    path: Path | None = None

    # ---- derived ----

    # How a profession writes a title and how you type it are rarely the same.
    # An accountant searching "fp&a" missed "Financial Planning & Analysis
    # Manager", "FP&A Manager" and "Head of Financial Planning and Analysis":
    # 35 real roles, a 76% difference in results, with nothing to indicate it.
    ABBREVIATIONS = {
        "fp&a": "financial planning and analysis",
        "hr": "human resources",
        "qa": "quality assurance",
        "pm": "project manager",
        "bd": "business development",
        "sre": "site reliability engineer",
        "ml": "machine learning",
        "bi": "business intelligence",
        "ux": "user experience",
        "cs": "customer success",
        "m&a": "mergers and acquisitions",
        "ap": "accounts payable",
        "ar": "accounts receivable",
        # Missing entirely, and it is the one that costs most. A config asking
        # for "vp engineering" matched "VP of Engineering" and missed "Vice
        # President, Engineering", which is the form 22 of 165 leadership
        # postings in one sample use. Both directions come free: somebody who
        # types the long form now matches the short one too.
        "vp": "vice president",
        "svp": "senior vice president",
        "evp": "executive vice president",
        "coo": "chief operating officer",
        "cto": "chief technology officer",
        "cfo": "chief financial officer",
        "ceo": "chief executive officer",
        "cpo": "chief product officer",
        "ciso": "chief information security officer",
    }

    @staticmethod
    def _title_variants(term: str) -> set[str]:
        """Every spelling of one title worth matching.

        Covers the "&" / "and" split and the common abbreviations, in both
        directions, so it does not matter which form you typed.
        """
        out = {" ".join(term.lower().split())}
        # Two passes, because expanding an abbreviation can introduce an "and"
        # that then needs its own "&" form: fp&a -> financial planning and
        # analysis -> financial planning & analysis.
        for _ in range(2):
            for v in list(out):
                for short, long in Config.ABBREVIATIONS.items():
                    # On word boundaries. A bare `in` matched inside other
                    # words and then replaced there: "ar" is inside
                    # "marketing", so "marketing manager" expanded to
                    # "maccounts receivableketing manager", and "pm" inside
                    # "shipment" gave "shiproject managerent coordinator".
                    # Harmless in that nothing matched them, and pure junk in
                    # a compiled title pattern, which is where the next
                    # unexplained match will come from.
                    short_re = rf"\b{re.escape(short)}\b"
                    long_re = rf"\b{re.escape(long)}\b"
                    if re.search(short_re, v):
                        out.add(re.sub(short_re, long, v))
                    if re.search(long_re, v):
                        out.add(re.sub(long_re, short, v))
            for v in list(out):
                for a, b in (("&", " and "), (" and ", " & ")):
                    if a in v:
                        out.add(" ".join(v.replace(a, b).split()))
        return {" ".join(v.split()) for v in out if v.strip()}

    def title_terms_expanded(self) -> list[str]:
        """Every configured title, plus every spelling of it.

        The loose matcher was handed the RAW configured terms while the regex
        beside it was handed the expanded ones, so an abbreviation only ever
        worked in the strict path. "vp engineering" matched "VP of
        Engineering", because the regex tolerates the "of", and missed "Vice
        President, Engineering" entirely, which is the form 22 of 165
        leadership postings in one sample use. The loose matcher is the half
        that handles a reordered or interrupted title, and it was the half
        that could not read the abbreviation.
        """
        out: set[str] = set()
        for term in self.titles_include:
            out |= self._title_variants(term)
        return sorted(out)

    def title_include_re(self):
        if not self.titles_include:
            return None
        variants: set[str] = set(self.title_terms_expanded())
        return re.compile(
            "|".join(rf"\b{re.escape(v)}\b" for v in sorted(variants, key=len, reverse=True)),
            re.I)

    @staticmethod
    def _bounded(term: str) -> str:
        """One exclusion term, escaped and held to whole words.

        \\b only works next to a word character, so the boundary is added on
        each end only when that end is alphanumeric: a term ending in ")"
        would never match if it were added unconditionally.
        """
        esc = re.escape(term)
        lead = r"\b" if term[:1].isalnum() else ""
        trail = r"\b" if term[-1:].isalnum() else ""
        return f"{lead}{esc}{trail}"

    def title_exclude_re(self):
        """Escaped, unlike the include list which is also escaped.

        Left unescaped, a genuine job title containing brackets became a
        regex: "healthcare assistant (bank)" then failed to match the real
        posting and matched a different one instead.
        """
        if not self.titles_exclude:
            return None
        return re.compile("|".join(self._bounded(t) for t in self.titles_exclude),
                          re.I)

    def location_exclude_re(self):
        """Whole words, for the same reason the title list is.

        This was a bare substring match, and a substring of a place name is
        usually a different place: `exclude: [Bath]` silently dropped a role
        in Bathgate, 400 miles away, and the only trace was one more in the
        "location excluded" count. "Not London" is the most load-bearing line
        a UK user writes and the failure it produced looked exactly like it
        working.
        """
        if not self.exclude_locations:
            return None
        return re.compile("|".join(self._bounded(x) for x in self.exclude_locations),
                          re.I)


class ConfigError(ValueError):
    """A config problem worth stopping for, phrased for the person who wrote it."""


def _num(v, key: str):
    """Accept what a person actually types for money.

    `floor: £70,000` and `floor: 70,000` both parse as strings in YAML, were
    stored unconverted, and then raised TypeError deep inside the salary
    comparison partway through a scan.
    """
    if v is None or isinstance(v, (int, float)):
        return v
    s = str(v).strip().replace(",", "").replace("£", "").replace("$", "").replace("€", "")
    s = re.sub(r"(?i)\s*(per annum|pa|k)$", lambda m: "000" if m.group(1).lower() == "k" else "", s)
    try:
        return float(s)
    except ValueError:
        raise ConfigError(
            f"{key}: {v!r} is not a number. Write it plainly, like 70000.")


def _int(v, key: str, default: int) -> int:
    """A whole number, or a ConfigError that names the key it came from.

    `fetch.concurrency: loads` raised a bare `ValueError: invalid literal for
    int() with base 10: 'loads'` out of `load()`. Every other bad value in this
    file produces a sentence naming the setting and saying what to write; this
    one produced a Python traceback with no mention of `fetch`, of
    `concurrency`, or of which file it was reading.
    """
    if v is None:
        return default
    if isinstance(v, bool):
        # `retries: yes` is a YAML boolean, and int(True) is 1. That is a
        # number, so nothing would have complained, and the setting would have
        # meant something the writer did not ask for.
        raise ConfigError(f"{key}: {v!r} is true or false, not a number. "
                          f"Write it plainly, like {default}.")
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ConfigError(f"{key}: {v!r} is not a whole number. "
                          f"Write it plainly, like {default}.")


def _block(raw: dict, name: str) -> dict:
    """One section of the file, or a ConfigError that names it.

    `titles:` written as a YAML list is the obvious mistake, because what goes
    under it IS a list of titles. It reached `.get` and came back as
    "'list' object has no attribute 'get'": no mention of `titles`, no mention
    of the config file, and it reads like a bug in the tool rather than a typo
    in the file. `locations:` did the same.
    """
    block = raw.get(name)
    if block is None:
        return {}
    if not isinstance(block, dict):
        keys = sorted(KNOWN_KEYS.get(name, ()))
        hint = f" The keys that go under it: {', '.join(keys)}." if keys else ""
        raise ConfigError(
            f"{name}: expected indented `key: value` settings, not a "
            f"{type(block).__name__}.{hint}")
    return block


def _bool(v, key: str) -> bool:
    """`remote_ok: "no"` used to mean yes, because bool("no") is True."""
    if isinstance(v, bool):
        return v
    if v is None:
        return True
    s = str(v).strip().lower()
    if s in ("true", "yes", "y", "1", "on"):
        return True
    if s in ("false", "no", "n", "0", "off"):
        return False
    raise ConfigError(f"{key}: {v!r} is not true or false.")


def _as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return list(v)
    return [v]


def _terms(values) -> list[str]:
    """Search terms, with the blank ones dropped.

    A YAML list with an empty entry in it -- a bare `- `, or `- ""` -- put an
    empty string in `titles.include`. That is not a title nobody matches, it
    is a title EVERYBODY matches: `title_include_re` joins the terms into one
    pattern, an empty term contributes an empty alternative, and an empty
    regex matches every string there is. So the title gate stopped dropping
    anything, `score` awarded every posting its 35 points for "title matches
    your targets", and the scan reported a very large number of matches and
    looked like it had worked.

    The `titles.include is empty` guard did not catch it, because the list was
    not empty. Stripping here is what makes that guard true again.
    """
    # `v is not None` before the string test, because a bare `- ` in YAML
    # parses as None and `str(None)` is the four-character title "None",
    # which is a term that matches a posting rather than one that is skipped.
    return [" ".join(str(v).split()) for v in _as_list(values)
            if v is not None and str(v).strip()]


VALID_FORMATS = {"html", "json", "markdown", "md"}

KNOWN_KEYS = {
    "titles": {"include", "exclude"},
    "locations": {"countries", "remote_ok", "work_modes", "relocate_to", "exclude",
                  "need_sponsorship"},
    "salary": {"floor", "currency"},
    "cv": {"path"},
    "sources": {"use_bundled", "countries", "extra", "reed_api_key",
                "adzuna_app_id", "adzuna_app_key"},
    "output": {"formats", "dir"},
    "fetch": {"concurrency", "timeout", "retries", "user_agent"},
}
TOP_LEVEL = set(KNOWN_KEYS) | {"dealbreakers", "sectors"}


# Every country the location filter can recognise, plus the names people
# actually type. `countries: [Portugal]` used to match nothing at all: the
# filter compares against internal codes, so a Lisbon user asking for
# Portugal, Spain, Netherlands and Germany got 112 UK roles, zero from any
# country they asked for, and no warning. Names are accepted and normalised
# rather than rejected, because typing your country's name is the reasonable
# thing to do.
COUNTRY_ALIASES = {
    "united kingdom": "UK", "great britain": "UK", "britain": "UK",
    "gb": "UK", "gbr": "UK", "england": "UK", "scotland": "UK",
    "wales": "UK", "northern ireland": "UK",
    "united states": "US", "usa": "US", "america": "US",
    "ireland": "IE", "eire": "IE", "germany": "DE", "deutschland": "DE",
    "france": "FR", "spain": "ES", "espana": "ES", "netherlands": "NL",
    "holland": "NL", "canada": "CA", "australia": "AU", "new zealand": "NZ",
    "uae": "AE", "united arab emirates": "AE", "singapore": "SG",
    "hong kong": "HK", "india": "IN", "japan": "JP", "china": "CN",
    "poland": "PL", "portugal": "PT", "sweden": "SE", "switzerland": "CH",
    "israel": "IL", "brazil": "BR", "mexico": "MX", "south africa": "ZA",
    "indonesia": "ID", "thailand": "TH", "malaysia": "MY",
    "philippines": "PH", "italy": "IT", "italia": "IT", "belgium": "BE",
    "austria": "AT", "denmark": "DK", "norway": "NO", "finland": "FI",
    "czech republic": "CZ", "czechia": "CZ", "romania": "RO",
    "turkey": "TR", "argentina": "AR", "vietnam": "VN", "south korea": "KR",
}

# Codes the pipeline uses, taken from the filter itself so the two cannot
# drift. Imported lazily to keep config.py free of a screen.py dependency.
def _known_country_codes() -> set[str]:
    """Every country the tool can actually decide a role is in.

    This read `_COUNTRY_MARKERS` alone, which is 40 codes: the countries whose
    NAME the location logic recognises in text. But a role is also placed by
    its city, and the city tables know 92 and 86 more, so `enrich` routinely
    returns a country the config would refuse.

    The published seed makes the gap countable: 162 country shards, of which
    122 no config could ask for, holding 20,752 roles. Greece has 3,396 and
    `countries: [GR]` was an error message. Saudi Arabia has 2,225, Colombia
    1,821.

    So the answer comes from the union of what the placer can produce.
    `_CANONICAL_NAME` is the widest of them and is what `_country_of` maps
    into, which makes it the honest source rather than the largest one.
    """
    from .screen import (_CANONICAL_NAME, _CITY_HINTS, _COUNTRY_MARKERS,
                         _MORE_CITIES)
    codes: set[str] = set()
    for table in (_COUNTRY_MARKERS, _CITY_HINTS, _MORE_CITIES,
                  _CANONICAL_NAME):
        codes |= {k for k in table
                  if isinstance(k, str) and len(k) == 2 and k.isupper()}
    # UK is this tool's spelling and is what every message and config uses.
    codes.add("UK")
    return codes


def _name_to_code(text: str) -> str | None:
    """A country's own name turned into its code, or None."""
    from .screen import _COUNTRY_NAME, _fold
    t = " ".join(str(text or "").split()).lower()
    return _COUNTRY_NAME.get(t) or _COUNTRY_NAME.get(_fold(t))


def _countries(values, where: str) -> list[str]:
    """Normalise a country list, and refuse anything the filter cannot use.

    Silence here is the worst possible behaviour: an unrecognised entry does
    not loosen the filter, it removes that country from it entirely, and the
    results still look like a working scan.
    """
    known = _known_country_codes()
    out = []
    for v in values:
        # YAML 1.1, which is what PyYAML speaks, reads an unquoted NO as the
        # boolean false. So `countries: [NO]` reached here as `[False]`, and a
        # Norwegian typing the correct ISO code was told "'False' is not a
        # country this tool recognises. Did you mean FI, FR?". Norway has 323
        # roles in the published seed and was unreachable through any config.
        #
        # Turned back rather than refused, because among ISO country codes the
        # mapping is not ambiguous: NO is the only one YAML eats into false.
        # ON, OFF, YES and TRUE are not countries, so a true has no country
        # it could have been, and that is worth saying with the fix rather
        # than guessing one the reader never typed.
        if v is False:
            v = "NO"
        elif v is True:
            raise ConfigError(
                f"{where}: YAML read one of these as the boolean true, which "
                f"happens to an unquoted ON, YES or TRUE. None of those is a "
                f"country code. Quote it if you meant a country: [\"ON\"].")
        t = str(v).strip()
        if not t:
            continue
        # The hand-written alias table FIRST, because it carries the
        # spellings this tool decided on ("gb" means UK here), then the
        # placer's own name map, which knows all 170.
        #
        # The codes were widened to 170 and this was left at 57, so 129
        # countries were accepted by code and refused by name.
        # `--countries Greece` wrote a config that `list` then rejected with
        # "'GREECE' is not a country this tool recognises. Did you mean GA,
        # GE, GH, GM?", none of which is Greece. The map that knows
        # "greece" is GR was already imported by the function three lines
        # above.
        code = COUNTRY_ALIASES.get(t.lower()) or _name_to_code(t) or t.upper()
        if code not in known:
            near = [c for c in sorted(known) if c.startswith(t[:1].upper())]
            hint = f" Did you mean {', '.join(near[:4])}?" if near else ""
            raise ConfigError(
                f"{where}: {t!r} is not a country this tool recognises.{hint} "
                f"Use a two-letter code (or UK). Valid: {', '.join(sorted(known))}")
        if code not in out:
            out.append(code)
    return out


# Only currencies the salary parser can actually produce. `currency: euro`
# uppercased to EURO, never matched EUR, and silently switched the floor off
# on every euro role.
#
# Taken FROM the parser rather than written out here, because a hand-kept
# second copy drifts and the drift is silent in the dangerous direction. This
# was `{"GBP", "USD", "EUR"}` while the parser could already produce INR, SGD,
# AED, CAD and the rest from the job's own country, so an Indian or Singapore
# reader could not state a floor at all. The error message was clear and had
# no way forward, and the workaround it pushed people to -- pick USD -- is
# exactly what makes a mis-stamped currency dangerous: a floor in USD compared
# against a figure the parser has now correctly labelled SGD is refused as a
# cross-currency comparison, which is safe, but a floor in USD against a
# figure WRONGLY labelled USD deletes the role.
VALID_CURRENCIES = set(KNOWN_CURRENCIES)

# What `screen.work_mode` can answer, which is what this can filter on.
VALID_WORK_MODES = {"remote", "hybrid", "office"}


def _work_modes(v, where: str) -> list[str]:
    """The arrangements to keep. Empty means no filter.

    "unstated" is deliberately not accepted, because it is not a choice
    anybody makes: a posting that does not say is kept whatever this is set
    to, and flagged. Half of all postings do not say, and reading "we cannot
    tell" as "not remote" would hide more real remote roles than it removed
    office ones.
    """
    out = []
    for item in _as_list(v):
        t = str(item or "").strip().lower()
        if t in ("on-site", "onsite", "in office", "in-office"):
            t = "office"
        if t == "unstated":
            raise ConfigError(
                f"{where}: postings that do not state an arrangement are "
                f"always kept and flagged, so listing 'unstated' here would "
                f"change nothing. Remove it.")
        if t not in VALID_WORK_MODES:
            raise ConfigError(
                f"{where}: {item!r} is not a working arrangement. "
                f"Valid: {', '.join(sorted(VALID_WORK_MODES))}.")
        out.append(t)
    return list(dict.fromkeys(out))
_CURRENCY_ALIASES = {"POUND": "GBP", "POUNDS": "GBP", "STERLING": "GBP",
                     "EURO": "EUR", "EUROS": "EUR", "DOLLAR": "USD",
                     "DOLLARS": "USD", "US$": "USD", "USDOLLAR": "USD"}


def _currency(v, where: str) -> str:
    t = str(v or "GBP").strip().upper()
    t = _CURRENCY_ALIASES.get(t, t)
    if t not in VALID_CURRENCIES:
        raise ConfigError(
            f"{where}: {v!r} is not a currency this tool compares. "
            f"Valid: {', '.join(sorted(VALID_CURRENCIES))}. Salaries in any "
            f"other currency are shown and never compared to your floor.")
    return t


def _api_key(value, env_var: str) -> str:
    """A credential, from the config file or from the environment.

    Both, because this repo has two kinds of user and neither route serves the
    other. Locally the key belongs in config.local.yaml, which is gitignored;
    in GitHub Actions there is no local file at all and the key arrives as a
    secret in the environment. Reading only one of the two strands the other.

    The file wins when it has a value, so a stale export in a shell cannot
    quietly override the key someone just wrote down. Empty means no key, and
    the source that needs one is skipped and says so, rather than being
    fetched into a 401 that reads like a broken board.
    """
    v = str(value or "").strip()
    return v or os.environ.get(env_var, "").strip()


def _bundled_sector_tags() -> set[str] | None:
    """Every sector tag in the bundled list, or None if it cannot be read.

    None means "do not judge", not "no tags". A missing or corrupt source list
    is its own failure and refusing every sector on the back of it would be a
    second, wronger one.
    """
    from .sources import BUNDLED
    import json as _json
    try:
        raw = _json.loads(BUNDLED.read_text(encoding="utf-8"))
        items = raw.get("sources", raw) if isinstance(raw, dict) else raw
        known = {(d.get("sector") or "").lower() for d in items if isinstance(d, dict)}
        known.discard("")
        return known
    except (OSError, ValueError):
        return None


def _sectors(values) -> list[str]:
    """Refuse a sector tag that is not in the bundled list.

    `sectors: [hospitality]` is the obvious thing for a restaurant manager to
    write. It is not a tag, so it matched nothing, switched off 299 of 307
    sources, and still printed a normal-looking scan.
    """
    known = _bundled_sector_tags()
    if known is None:
        return [str(v).strip().lower() for v in values if str(v).strip()]
    out = []
    for v in values:
        t = str(v).strip().lower()
        if not t:
            continue
        if t not in known:
            raise ConfigError(
                f"sectors: {v!r} is not a tag in the bundled source list, so "
                f"it would match no employers at all. Valid: "
                f"{', '.join(sorted(known))}. Leave `sectors` empty to watch "
                f"every employer, and use `job-radar discover <employer> "
                f"--add` to add your own.")
        if t not in out:
            out.append(t)
    return out


# What one `sources.extra` entry may say. Taken from `Source.from_dict`, which
# is the code that actually reads it.
EXTRA_SOURCE_KEYS = {"company", "url", "platform", "sector", "country",
                     "domain", "method", "body", "keyword_template",
                     "employment"}

# A source is fetched exactly as written, so this is the whole test of whether
# it could ever be fetched at all.
_URL_RE = re.compile(r"^https?://\S+$", re.I)


def _extra_sources(values, where: str) -> list[dict]:
    """Validate the one config block that nothing was checking.

    Every other block in this file is validated. This one was passed straight
    through to `Source.from_dict`, and each of these was live:

      * `company:` typed `compny:` raised `KeyError: 'company'` out of
        `sources.load`, which is EVERY command that reads a source list. The
        traceback named no entry, no key and no file.
      * a bare `- hello` became an employer called hello with a board at the
        url hello. It cannot answer, and a board that cannot answer counts in
        the summary exactly like a board with no vacancies.
      * `platform: not-a-real-platform` was accepted and then ignored, so the
        board was parsed as whatever its URL looked like.
      * `url: just some text` was accepted.
      * `country: Mars` was quietly rewritten to `unknown` by
        `sources.normalise_country_tag`, while the same word under
        `locations.countries` is refused outright. The two disagreed about the
        same mistake, and the silent one is the one that loses roles.

    The country is normalised here as well as checked, so `country: Germany`
    reaches `sources.load` as DE rather than being turned into `unknown` on
    the way past.
    """
    from . import adapters
    from .sources import NON_COUNTRY_TAGS, _COUNTRY_TAG_SYNONYMS

    sector_tags = None
    out = []
    for i, entry in enumerate(_as_list(values)):
        label = f"{where}[{i}]"
        if isinstance(entry, str):
            # `sources.load` turns a bare string into company=url=the string,
            # which is only ever meaningful when the string is a board URL.
            if not _URL_RE.match(entry.strip()):
                raise ConfigError(
                    f"{label}: {entry!r} is not a URL, so it would become an "
                    f"employer called {entry!r} with a board at {entry!r} that "
                    f"nothing can fetch. Write it as `- company: Name` with "
                    f"`url: https://...` indented under it.")
            entry = {"company": entry.strip(), "url": entry.strip()}
        if not isinstance(entry, dict):
            raise ConfigError(
                f"{label}: expected `company:` and `url:`, not a "
                f"{type(entry).__name__}.")

        d = dict(entry)
        name = d.get("company")
        if isinstance(name, str) and name.strip():
            label = f"{where} '{name.strip()}'"

        unknown = set(d) - EXTRA_SOURCE_KEYS
        if unknown:
            raise ConfigError(
                f"{label}: unknown key(s) {sorted(unknown)}. "
                f"Valid: {sorted(EXTRA_SOURCE_KEYS)}.")

        if not (isinstance(name, str) and name.strip()):
            raise ConfigError(
                f"{label}: no `company`. That is the name this board is "
                f"reported and de-duplicated under, and without it every "
                f"command that loads sources stops on a KeyError naming "
                f"nothing at all.")
        d["company"] = name.strip()

        url = d.get("url")
        if not (isinstance(url, str) and _URL_RE.match(url.strip())):
            raise ConfigError(
                f"{label}: `url` must be an http or https address, not "
                f"{url!r}. It is fetched exactly as written.")
        d["url"] = url.strip()

        plat = d.get("platform")
        if plat is not None and str(plat).strip():
            known = set(adapters.platform_names()) | {"custom"}
            if str(plat).strip().lower() not in known:
                raise ConfigError(
                    f"{label}: {plat!r} is not a platform this tool has an "
                    f"adapter for, so the board would be read with the wrong "
                    f"parser and come back with no postings, which looks "
                    f"exactly like an employer with no vacancies. Valid: "
                    f"{', '.join(sorted(known))}. Leave `platform` out "
                    f"entirely to have it worked out from the URL.")
            d["platform"] = str(plat).strip().lower()
        else:
            d.pop("platform", None)

        cc = d.get("country")
        if cc is not None and str(cc).strip():
            t = str(cc).strip()
            tag = _COUNTRY_TAG_SYNONYMS.get(t.lower())
            if tag in NON_COUNTRY_TAGS:
                d["country"] = tag
            else:
                # Same refusal as `locations.countries`, and the same message.
                d["country"] = _countries([t], f"{label}.country")[0]
        else:
            d.pop("country", None)

        sec = d.get("sector")
        if sec is not None and str(sec).strip():
            if sector_tags is None:
                sector_tags = _bundled_sector_tags()
            t = str(sec).strip().lower()
            if sector_tags and t not in sector_tags:
                raise ConfigError(
                    f"{label}: {sec!r} is not a tag in the bundled source "
                    f"list, so this board would be dropped by any `sectors:` "
                    f"filter you set and kept by none of them. Valid: "
                    f"{', '.join(sorted(sector_tags))}. Leave `sector` out to "
                    f"have the board kept whatever `sectors:` says.")
            d["sector"] = t
        else:
            d.pop("sector", None)

        method = d.get("method")
        if method is not None and str(method).strip().upper() not in ("GET", "POST"):
            raise ConfigError(
                f"{label}: `method` is {method!r}; only GET and POST are sent.")
        if method is not None:
            d["method"] = str(method).strip().upper()

        if "body" in d and d["body"] is not None and not isinstance(d["body"], dict):
            raise ConfigError(
                f"{label}: `body` is the JSON posted to the board and must be "
                f"a block of `key: value`, not a {type(d['body']).__name__}.")

        dom = d.get("domain")
        if dom is not None and not (isinstance(dom, str) and dom.strip()):
            raise ConfigError(
                f"{label}: `domain` is the employer's own website, used to "
                f"check the board really is theirs. Give one or leave it out.")

        if "keyword_template" in d:
            d["keyword_template"] = _bool(d["keyword_template"],
                                          f"{label}.keyword_template")
            if d["keyword_template"] and "{keyword}" not in d["url"]:
                # `expand_templates` fills `{keyword}` in and produces one
                # search per title. A URL with no placeholder produces the
                # same URL per title, so the identical board is fetched up to
                # twelve times and de-duplicated back to one afterwards.
                raise ConfigError(
                    f"{label}: `keyword_template: true` but the url has no "
                    f"`{{keyword}}` in it, so it would be fetched once per "
                    f"title and every copy would be the same request.")

        if "employment" in d:
            # A source may declare what its own query already filtered for,
            # for platforms that filter at the request and send no per-result
            # flag. Refused unless it is one of the three real values: a typo
            # here would label every row the source returns, and label them
            # wrongly, with nothing on the row to show where it came from.
            from .employment import VALUES as _EMP_VALUES
            v = d["employment"]
            if not isinstance(v, str) or v.strip().lower() not in _EMP_VALUES:
                raise ConfigError(
                    f"{label}: `employment` must be one of "
                    f"{', '.join(sorted(_EMP_VALUES))}, and only where the "
                    f"URL's own filter has been checked to actually filter. "
                    f"Every posting this source returns is labelled with it.")
            d["employment"] = v.strip().lower()

        out.append(d)
    return out


def _dealbreakers(rows) -> list[Dealbreaker]:
    """Refuse quietly-broken entries rather than dropping them.

    A missing `pattern`, a typo'd key, or a regex that does not compile used to
    vanish without a word, so a dealbreaker you thought was protecting you was
    simply absent.
    """
    out = []
    for i, d in enumerate(rows or []):
        if not isinstance(d, dict):
            raise ConfigError(f"dealbreakers[{i}]: expected a name and a pattern.")
        name = d.get("name") or f"dealbreakers[{i}]"
        unknown = set(d) - {"name", "pattern", "hard"}
        if unknown:
            raise ConfigError(
                f"dealbreakers '{name}': unknown key(s) {sorted(unknown)}. "
                f"Did you mean 'pattern'?")
        if not d.get("pattern"):
            raise ConfigError(f"dealbreakers '{name}': no pattern, so it would "
                              f"never match anything.")
        try:
            re.compile(d["pattern"], re.I)
        except re.error as e:
            raise ConfigError(f"dealbreakers '{name}': the pattern is not a "
                              f"valid regular expression ({e}).")
        out.append(Dealbreaker(name=name, pattern=d["pattern"],
                               hard=_bool(d.get("hard", True),
                                          f"dealbreakers '{name}'.hard")))
    return out


def _check_keys(raw: dict) -> None:
    """Catch `sector:` for `sectors:` at load, not by wondering why nothing
    filtered."""
    unknown = set(raw) - TOP_LEVEL
    if unknown:
        raise ConfigError(f"unknown setting(s) {sorted(unknown)}. "
                          f"Valid: {sorted(TOP_LEVEL)}")
    for section, allowed in KNOWN_KEYS.items():
        # `_block`, not `isinstance(..., dict)`. The old test skipped a
        # section written as a list without a word, which is how
        # "'list' object has no attribute 'get'" got as far as `load`.
        block = _block(raw, section)
        if block:
            extra = set(block) - allowed
            if extra:
                raise ConfigError(f"{section}: unknown key(s) {sorted(extra)}. "
                                  f"Valid: {sorted(allowed)}")


def load(path: str | os.PathLike | None = None) -> Config:
    p = resolve(path)
    if not p.exists():
        raise FileNotFoundError(
            f"No config at {p}. Run `job-radar setup` to create one, "
            f"or copy config.example.yaml."
        )
    raw: dict[str, Any] = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    _check_keys(raw)

    titles = _block(raw, "titles")
    loc = _block(raw, "locations")
    sal = _block(raw, "salary")
    src = _block(raw, "sources")
    out = _block(raw, "output")
    fet = _block(raw, "fetch")

    cfg = Config(
        titles_include=_terms(titles.get("include")),
        titles_exclude=_terms(titles.get("exclude")),
        countries=_countries(_as_list(loc.get("countries")), "locations.countries"),
        remote_ok=_bool(loc.get("remote_ok", True), "locations.remote_ok"),
        work_modes=_work_modes(loc.get("work_modes"), "locations.work_modes"),
        relocate_to=_countries(_as_list(loc.get("relocate_to")), "locations.relocate_to"),
        need_sponsorship=_countries(_as_list(loc.get("need_sponsorship")),
                                    "locations.need_sponsorship"),
        exclude_locations=_as_list(loc.get("exclude")),
        salary_floor=_num(sal.get("floor"), "salary.floor"),
        salary_currency=_currency(sal.get("currency"), "salary.currency"),
        dealbreakers=_dealbreakers(raw.get("dealbreakers")),
        sectors=_sectors(_as_list(raw.get("sectors"))),
        source_countries=_countries(_as_list(src.get("countries")), "sources.countries"),
        use_bundled_sources=_bool(src.get("use_bundled", True), "sources.use_bundled"),
        extra_sources=_extra_sources(src.get("extra"), "sources.extra"),
        reed_api_key=_api_key(src.get("reed_api_key"), "REED_API_KEY"),
        adzuna_app_id=_api_key(src.get("adzuna_app_id"), "ADZUNA_APP_ID"),
        adzuna_app_key=_api_key(src.get("adzuna_app_key"), "ADZUNA_APP_KEY"),
        cv_path=str(_block(raw, "cv").get("path") or ""),
        formats=_as_list(out.get("formats")) or ["html", "json"],
        out_dir=Path(out.get("dir") or "out"),
        concurrency=_int(fet.get("concurrency"), "fetch.concurrency",
                         DEFAULT_CONCURRENCY),
        timeout=_int(fet.get("timeout"), "fetch.timeout", 20),
        retries=_int(fet.get("retries"), "fetch.retries", 2),
        # Read, not just accepted. `user_agent` was in KNOWN_KEYS, so setting
        # it passed validation and told the user nothing was wrong, and then
        # the dataclass default won anyway: a config asking to identify itself
        # as something else was silently overruled. Accepting a setting and
        # ignoring it is worse than rejecting it, because the rejection is at
        # least visible.
        user_agent=str(fet.get("user_agent") or Config.user_agent),
        path=p,
    )

    if not cfg.titles_include:
        raise ConfigError(
            "titles.include is empty. It is required: with no titles every "
            "posting matches, and the keyword-driven sources (NHS Jobs, "
            "LinkedIn) have nothing to search for.")

    bad = [f for f in cfg.formats if f not in VALID_FORMATS]
    if bad:
        raise ConfigError(f"output.formats: {bad} is not a format. "
                          f"Valid: html, json, markdown.")

    cfg.out_dir = Path(str(cfg.out_dir)).expanduser()

    # Everything that writes a document needs the real CV to work from, and a
    # path that has silently stopped existing produces a fabricated CV rather
    # than an error. So it is checked here, on load, not at generation time.
    if cfg.cv_path:
        cv = Path(cfg.cv_path).expanduser()
        if not cv.exists():
            raise FileNotFoundError(
                f"Your CV is configured as {cv} but there is no file there.\n"
                f"Fix `cv.path` in {p}, or run `job-radar setup` again."
            )
        cfg.cv_path = str(cv.resolve())

    # This used to be capped at 12 because it was the only thing standing
    # between the tool and a burst against somebody's job board. It is not any
    # more: `fetch` paces each host on its own clock, so this number decides
    # how many DIFFERENT hosts are in flight, not how hard any one of them is
    # hit. On a list where roughly 7,748 hosts hold a single board each, a low
    # cap here bought no politeness at all and cost most of an hour.
    #
    # There is still a ceiling, because the sockets, file descriptors and DNS
    # lookups are the user's own and a four-figure number here just exhausts
    # them. 64 is high enough that the per-host limits are what bounds a scan
    # and low enough to stay inside a default macOS descriptor limit.
    if cfg.concurrency > MAX_CONCURRENCY:
        print(f"  ! concurrency {cfg.concurrency} capped to {MAX_CONCURRENCY} "
              f"(per-host pacing is what keeps this polite, but the sockets "
              f"are still yours)")
        cfg.concurrency = MAX_CONCURRENCY
    if cfg.concurrency < 1:
        print(f"  ! concurrency {cfg.concurrency} raised to 1")
        cfg.concurrency = 1
    return cfg
