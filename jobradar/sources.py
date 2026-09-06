"""Loading and filtering the employer list."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote_plus, urlparse

from . import adapters
from .config import Config
from .models import Source
from .screen import country_name
from .state import atomic_write_text

BUNDLED = Path(__file__).parent.parent / "sources" / "sources.json"


def load_file(path: str | Path) -> list[Source]:
    p = Path(path)
    if not p.exists():
        return []
    raw = json.loads(p.read_text(encoding="utf-8"))
    items = raw.get("sources", raw) if isinstance(raw, dict) else raw
    out = []
    for d in items:
        # Tolerate the n8n export shape, which wraps each entry in {"json": {...}}
        if isinstance(d, dict) and "json" in d and isinstance(d["json"], dict):
            d = d["json"]
        if not isinstance(d, dict) or not d.get("url"):
            continue
        s = adapters.prepare(Source.from_dict(d))
        # One spelling, decided here, so no consumer has to know the file once
        # held two of them.
        s.country = normalise_country_tag(s.country)
        out.append(s)
    return out


# How many of your titles a keyword platform is searched with.
#
# There is a cap because the request count is titles times countries times
# pages, and these are searches rather than one board each. There is a cap of
# TWELVE rather than six because six silently stopped searching for half of a
# realistic config: eleven titles is normal once you ask for leadership rather
# than for the exact words "engineering manager", and the cut was made without
# saying so, which is the same silent truncation this tool keeps finding
# elsewhere. `dropped_titles` exists so the caller can say what it skipped.
MAX_KEYWORD_TITLES = 12


def dropped_titles(titles: list[str]) -> list[str]:
    """The titles a keyword search will not be run for, so a caller can say so."""
    return list(titles[MAX_KEYWORD_TITLES:])


# The only placeholders a source URL may carry: one of your titles, and one of
# the places you would work. Nothing else is known here, so nothing else can
# be filled in.
TEMPLATE_FIELDS = ("keyword", "country")
_FIELD_LIST = " and ".join("{" + f + "}" for f in TEMPLATE_FIELDS)


class UnusableSourceURL(ValueError):
    """A source URL that cannot be turned into a real one."""


def fill_template(url: str, keyword: str, country: str = "") -> str:
    """Fill a templated source URL, or say plainly why it cannot be filled.

    `str.format` raises on any placeholder it was not given, and one source
    carrying `&loc={location}` was enough to kill a whole `validate` run: the
    KeyError came back out of `ThreadPoolExecutor.map`, so every source after
    it went unchecked and the health check reported nothing about them either
    way. A literal `{` in a query string does the same thing, and a
    hand-written `sources.extra` entry is exactly where one turns up.

    One unusable URL is a fact about one source. Raising a named error lets
    the caller report that source and carry on with the rest.
    """
    try:
        return url.format(keyword=keyword, country=country)
    except KeyError as e:
        raise UnusableSourceURL(
            f"the URL asks for {{{e.args[0]}}}, which nothing here can fill "
            f"in; only {_FIELD_LIST} are known") from e
    except IndexError as e:
        raise UnusableSourceURL(
            f"the URL has an empty or numbered {{}} placeholder; only "
            f"{_FIELD_LIST} are known") from e
    except ValueError as e:
        raise UnusableSourceURL(f"the URL has a malformed placeholder: {e}") from e


def url_template_error(src: Source) -> str | None:
    """Why this source's URL cannot be turned into a real one, or None.

    Asked only of the sources something actually formats, which is the same
    condition `discover.validate_source` uses. A plain board URL is never
    formatted, so a literal brace in one is harmless and must not be reported
    as a fault.
    """
    if not (src.keyword_template or "{keyword}" in src.url):
        return None
    try:
        fill_template(src.url, keyword="test")
    except UnusableSourceURL as e:
        return str(e)
    return None


# One spelling for "this board is not in a single country".
#
# The bundled list carried both `multi` and `multiple`, and `cli.py` read both,
# so nothing was broken and nothing said which was right. That is a trap for
# the next reader and for the next consumer, which will handle whichever one
# it happened to see. `unknown` is the other non-country tag and means the
# harvester could not tell; neither may ever be stored as a country.
MULTI_COUNTRY = "multi"
NON_COUNTRY_TAGS = frozenset({MULTI_COUNTRY, "unknown"})
_COUNTRY_TAG_SYNONYMS = {
    "multiple": MULTI_COUNTRY, "multi": MULTI_COUNTRY,
    "global": MULTI_COUNTRY, "worldwide": MULTI_COUNTRY,
    "international": MULTI_COUNTRY, "various": MULTI_COUNTRY,
    "unknown": "unknown", "": "",
}


def normalise_country_tag(tag: str | None) -> str:
    """The one spelling of a board's country tag.

    A two-letter code is upper-cased and kept. Anything meaning "more than one
    country" becomes `multi`. Anything else unrecognised becomes `unknown`
    rather than being passed through, because a tag that is not a country and
    is not one of these two is a country to every reader downstream.
    """
    t = (tag or "").strip()
    if not t:
        return ""
    if len(t) == 2 and t.isalpha():
        return t.upper()
    return _COUNTRY_TAG_SYNONYMS.get(t.lower(), "unknown")


def expand_templates(srcs: list[Source], titles: list[str],
                     countries: list[str] | None = None,
                     problems: list | None = None) -> list[Source]:
    """Turn one templated source into one search per thing you care about.

    Some platforms are searches, not employer boards: NHS Jobs and LinkedIn
    return whatever keyword you give them. Shipping those as fixed URLs shipped
    the author's own job titles, so a nurse running this got eight searches for
    "engineering manager" inside the NHS and zero results out of 24,719
    postings. The keywords have to come from the user.

    `{country}` is the same argument one step further out. Workable's search
    covers every country it hosts, and "software engineer" worldwide is 4,220
    postings, 211 pages behind an opaque cursor. The same search narrowed to
    the United Kingdom is 322. Narrowing at the query is the difference
    between reading what the user asked for and reading the world and throwing
    almost all of it away, and it is the same reasoning that put
    `postedByDirectEmployer` in the Reed builder.

    A source with no `{country}` in it is unaffected, so NHS Jobs and LinkedIn
    keep expanding by title alone.

    `problems` collects `(company, why)` for the templates that cannot be
    filled in at all, so the caller can name them. They are skipped rather
    than raised: one hand-added source with `&loc={location}` in it used to
    take the whole scan down before a single board was read.
    """
    out: list[Source] = []
    for s in srcs:
        if not s.keyword_template:
            out.append(s)
            continue
        if not titles:
            continue          # nothing to search for; a frozen guess is worse
        bad = url_template_error(s)
        if bad:
            if problems is not None:
                problems.append((s.company, bad))
            continue
        wants_country = "{country}" in s.url
        # An unrecognised code would put a literal "XX" in the query and
        # return nothing, which reads as "no jobs there" rather than "that is
        # not a country". Falling back to one unfiltered search is honest: it
        # returns too much rather than nothing.
        names = [n for n in (country_name(c) for c in (countries or [])) if n]
        places = names if (wants_country and names) else [""]
        for title in titles[:MAX_KEYWORD_TITLES]:
            kw = quote_plus(title)
            for place in places:
                url = fill_template(s.url, keyword=kw,
                                    country=quote_plus(place))
                label = f"{s.company}: {title}"
                if place:
                    label += f" in {place}"
                out.append(Source(
                    company=label, url=url,
                    platform=s.platform, sector=s.sector,
                    country=s.country, domain=s.domain,
                    method=s.method, body=s.body,
                    # The expansion dropped this, so an expanded search looked
                    # like an employer's own board to everything downstream.
                    # `coverage` counts keyword sources to warn that they
                    # return leads with no description and include agencies,
                    # and with the flag gone it counted none of them.
                    keyword_template=True,
                    # Same shape, same fix, found the same way. A Reed source
                    # scoped to `contract=true` declares `employment:
                    # contract`, and dropping it here meant every expanded
                    # search arrived unscoped: 100 postings known to be
                    # contract work were classified from their prose instead,
                    # which labelled 57, missed 42 and called one permanent.
                    # The declaration lives on the template and every search
                    # built from it inherits the same query.
                    employment=s.employment))
    return out



# Which pass of a scan a source belongs in.
#
# A scan reads 17,807 boards and takes the better part of an hour, and half
# the roles arrive in the first four minutes. Measured on a real board:
#
#   sweeps and every one-board host    4.5 min   1,884 roles   419/min
#   Ashby                              8.7 min     619 roles    71/min
#   Greenhouse                        13.6 min     970 roles    71/min
#   Workable boards                   49.9 min     219 roles   4.4/min
#
# Ninety five times the yield per minute in the first pass as in the last.
# Read as one interleaved lump none of it is usable until all of it is done,
# so somebody who opens the dashboard at five minutes sees nothing at all.
#
# The boundaries are not arbitrary, which is what makes them hold as the list
# grows: they are drawn by who owns the host. Phase 1 is everything nobody
# rate limits, because it sits on its own hostname. Workday puts its 1,489
# boards on 1,467 different hosts, iCIMS gives every customer one, and the
# cross-employer sweeps are a single request each. Phases 2 and 3 are the two
# big shared hosts, where thousands of boards queue behind one limit. Phase 4
# is Workable, which is its own problem and always will be.
#
# Every phase runs on every scan. A posting can appear and be filled inside a
# week, so a pass that is skipped is a role nobody ever saw, and that is a
# worse failure than a slow scan. The phases decide the ORDER, not what is
# read.
PHASES = (
    (1, "the fast ones", ()),
    (2, "Ashby", ("api.ashbyhq.com",)),
    (3, "Greenhouse", ("boards-api.greenhouse.io",)),
    (4, "Workable's own boards", ("apply.workable.com",)),
)


def phase_of(src: Source) -> int:
    """Which pass this source belongs in. Phase 1 is everything unclaimed."""
    host = urlparse(src.url).netloc
    for num, _, hosts in PHASES:
        if host in hosts:
            return num
    return 1


def in_phases(srcs: list[Source]) -> list[tuple[int, str, list[Source]]]:
    """The sources grouped into passes, in the order they should be read."""
    out = []
    for num, label, _ in PHASES:
        got = [s for s in srcs if phase_of(s) == num]
        if got:
            out.append((num, label, got))
    return out


def load(cfg: Config, problems: list | None = None) -> list[Source]:
    srcs: list[Source] = []
    if cfg.use_bundled_sources:
        srcs.extend(load_file(BUNDLED))
    for d in cfg.extra_sources:
        if isinstance(d, str):
            d = {"company": d, "url": d}
        s = adapters.prepare(Source.from_dict(d))
        s.country = normalise_country_tag(s.country)
        srcs.append(s)

    if cfg.sectors:
        want = {s.lower() for s in cfg.sectors}
        srcs = [s for s in srcs if not s.sector or s.sector.lower() in want]
    if cfg.source_countries:
        # `NON_COUNTRY_TAGS` too, not just the untagged ones.
        #
        # A board tagged `multi` is a multinational: it is not evidence that
        # the employer has nothing in your country, it is the absence of a
        # single answer. Keeping only the untagged and the exact match dropped
        # all 1,599 multi-tagged boards from `sources.countries: [NL]`,
        # 17,817 sources down to 5,374, and those are precisely the employers
        # most likely to have a Dutch vacancy. Nothing said a word.
        #
        # Same rule as the seed's `unplaced` and `multiple` shards, and for
        # the same reason: a tag that means "we cannot say" must never be read
        # as "no".
        want = {c.upper() for c in cfg.source_countries}
        srcs = [s for s in srcs
                if not s.country
                or s.country.lower() in NON_COUNTRY_TAGS
                or s.country.upper() in want]

    # The relocation countries too, not just the home one: a search
    # narrowed to where the user already is cannot find the roles that
    # `relocate_to` exists to find.
    places = list(dict.fromkeys(list(cfg.countries) + list(cfg.relocate_to)))
    srcs = expand_templates(srcs, cfg.titles_include, places, problems)

    seen, uniq = set(), []
    for s in srcs:
        if s.key in seen:
            continue
        seen.add(s.key)
        uniq.append(s)
    return uniq


def save(sources: list[Source], path: str | Path, meta: dict | None = None) -> None:
    """Write the list, merging metadata rather than replacing it.

    Replacing it meant the weekly prune silently deleted the provenance note,
    the version and the harvest counts, and the release process then told you
    to bump a version that no longer existed.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if p.exists():
        try:
            prev = json.loads(p.read_text(encoding="utf-8"))
            existing = prev.get("meta", {}) if isinstance(prev, dict) else {}
        except (json.JSONDecodeError, OSError):
            existing = {}
    # Counted here rather than passed in, because nothing was maintaining it.
    # The weekly `validate --prune` rewrites this file and had no reason to
    # think about a number in the header, so `meta.boards` drifted a little
    # further from the truth every Sunday until it read 17,834 for a list of
    # 17,807. Deriving it from what is actually being written is the only
    # version that cannot go stale.
    # An employer's own board, which is not the same as "not a keyword
    # template". A cross-employer sweep is neither: it expands per title for
    # nobody and it belongs to no employer. Counting it as a board took the
    # figure to 17,808 against a header saying 17,807, which is how it was
    # noticed. `directness` already knows which platforms are aggregators and
    # is the one place that judgement should live.
    from .screen import directness
    boards = sum(1 for x in sources
                 if not x.keyword_template and directness(x.platform) >= 2)
    body = {
        "meta": {**existing, **(meta or {}), "boards": boards},
        "sources": [s.to_dict() for s in
                    sorted(sources, key=lambda x: (x.platform, x.company.lower()))],
    }
    # Atomic. The weekly `validate --prune` rewrites 17,810 entries in
    # place, and the crawler that finds employers does not ship here, so a
    # write killed half way through destroys a list nothing in this repository
    # can rebuild. It would also read back with no `meta`, which is how the
    # provenance note and the version would go missing on the run after.
    atomic_write_text(p, json.dumps(body, indent=1, ensure_ascii=False))


def age_days(path=None) -> int | None:
    """How long since the bundled list was last checked against reality.

    The list is data and it rots: boards migrate between applicant tracking
    systems, tokens get renamed, companies are acquired. Revalidating found 23
    dead boards in one pass, 19 of which had simply moved ATS and were hiding
    762 live roles.

    The weekly job fixes that upstream. It cannot fix anyone's copy, which is
    the part nothing said out loud: a clone freezes its source list on the day
    it was cloned, and a fork only ever prunes its own, because the crawler
    that finds new employers is not in this repository. Either way the fix is
    a pull, and someone has to be told that.
    """
    from datetime import date
    try:
        meta = json.loads(Path(path or BUNDLED).read_text(encoding="utf-8")).get("meta", {})
    except (OSError, ValueError, AttributeError):
        return None
    stamp = meta.get("checked") or meta.get("validated")
    if not stamp:
        return None
    try:
        return (date.today() - date.fromisoformat(str(stamp)[:10])).days
    except ValueError:
        return None


def coverage(sources: list[Source]) -> dict:
    """Where the list is thin. This is what says which sector to harvest next."""
    by_sector: dict[str, int] = {}
    by_country: dict[str, int] = {}
    by_platform: dict[str, int] = {}
    for s in sources:
        by_sector[s.sector or "untagged"] = by_sector.get(s.sector or "untagged", 0) + 1
        by_country[s.country or "untagged"] = by_country.get(s.country or "untagged", 0) + 1
        by_platform[s.platform or "unknown"] = by_platform.get(s.platform or "unknown", 0) + 1
    return {
        "total": len(sources),
        "by_sector": dict(sorted(by_sector.items(), key=lambda x: -x[1])),
        "by_country": dict(sorted(by_country.items(), key=lambda x: -x[1])),
        "by_platform": dict(sorted(by_platform.items(), key=lambda x: -x[1])),
    }
