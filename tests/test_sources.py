"""What the bundled source list must never contain, and how a failure to reach
a board is told apart from a board that is gone.

Two subjects, one file, because they are the same fault seen twice: a name
that identifies nobody and a transport error read as a death certificate both
end with the dashboard telling the reader something untrue about an employer.

Nothing here touches the network. The one payload used is a saved fixture.

Provenance of the names these tests lock in: each was read off the employer's
own Workday careers page (`og:description`, `og:title`), the legal entity on a
live requisition (`hiringOrganization.name` in the CXS job detail), an Oracle
requisition's corporate blurb, or the `og:site_name` of the domain the board
is hosted on. That probing did not fetch or honour any robots.txt, on the
owner's standing instruction -- the same position `jobradar/enrich.py` records
for the pages the scanner reads. Requests were paced at roughly one per second
per host and abandoned after two failures on a host; no 403, CAPTCHA or JS
challenge was worked around, and boards that answered 429 were left alone.
"""

from __future__ import annotations

import json
import re
import ssl
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from jobradar import adapters, discover
from jobradar.fetch import Result, fetch_one, handshake_failure, ssl_backend
from jobradar.models import Source

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SOURCES = ROOT / "sources" / "sources.json"


def _load() -> dict:
    return json.loads(SOURCES.read_text(encoding="utf-8"))


def _sources() -> list:
    return _load()["sources"]


def _employer_boards() -> list:
    """Everything except the keyword templates, which are searches, not
    employers, and are not what `meta.boards` counts."""
    return [s for s in _sources() if not s.get("keyword_template")]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# --------------------------------------------------------------------------
# The list itself
# --------------------------------------------------------------------------
def test_the_bundled_list_still_holds_the_boards_its_own_meta_claims():
    d = _load()
    assert d["meta"]["boards"] == len(_employer_boards()), (
        f"meta.boards says {d['meta']['boards']} but the file holds "
        f"{len(_employer_boards())} employer boards")
    assert d["meta"]["boards"] == 17819


def test_no_board_is_named_after_a_url_or_a_bare_hostname():
    """A name that is a URL is a scrape that kept the wrong string.

    Deliberately NOT flagging every name with a dot in it: 79 of these are
    dot-brands the employer actually trades under, Checkout.com and Otter.ai
    among them, and a rule that renames those makes the list worse.
    """
    bad = [s for s in _sources()
           if re.match(r"^https?://|^www\.", (s.get("company") or "").strip(), re.I)]
    assert bad == [], [s["company"] for s in bad]


def test_no_board_carries_a_job_board_boilerplate_wrapper_in_its_name():
    """"Job Listings at X", "Open Positions at X", "Work At X". The employer
    is X; the rest is the page furniture the name was scraped out of."""
    rx = re.compile(r"^(?:job listings? at|jobs at|careers at|work at|"
                    r"current (?:openings|vacancies)|open positions? at|"
                    r"join (?:us|our team)|vacancies at)\b", re.I)
    bad = [s for s in _sources() if rx.match((s.get("company") or "").strip())]
    assert bad == [], [s["company"] for s in bad]


def test_no_name_carries_an_unescaped_html_entity():
    rx = re.compile(r"&(?:amp|lt|gt|quot|apos|nbsp|#\d+|#x[0-9a-f]+);", re.I)
    bad = [s for s in _sources() if rx.search(s.get("company") or "")]
    assert bad == [], [s["company"] for s in bad]


def test_no_name_ends_in_a_stray_separator_or_carries_scrape_whitespace():
    """A trailing "+" is left alone on purpose: Applus+ and Brandtech+ are
    spelled that way. A trailing comma, pipe, dash or bracket is not a name."""
    bad = []
    for s in _sources():
        n = s.get("company") or ""
        if re.search(r"[\-–—,;:|/\\(\[{]\s*$", n) or n != n.strip() \
                or re.search(r"\s{2,}", n):
            bad.append(n)
    assert bad == [], bad


def test_no_board_is_named_after_a_generic_careers_site_word():
    """"Careers", "Global", "Us", "External". Every one of these is the word
    the vendor put in the URL, not an employer anybody can look up."""
    generic = {"careers", "career", "jobs", "job", "external", "internal",
               "search", "global", "us", "corporate", "portal", "home",
               "apply", "vacancies", "openings", "recruiting", "talent",
               "employment", "opportunities", "emploi", "jobsearch",
               "talents", "opentalent", "clinicianjobs", "usijobs", "sjobs"}
    bad = [s["company"] for s in _sources()
           if _norm(s.get("company")) in {_norm(g) for g in generic}]
    assert bad == [], bad


# The one Oracle tenant still filed under its pod code. Its single live
# requisition is a Product Control Officer in Nigeria reconciling Calypso
# against a core banking ledger: a bank, and nothing on the board, in the
# requisition detail or in the site's own branding says which one. Named here
# rather than guessed, so that a NEW pod-coded board still fails this test.
UNIDENTIFIED_ORACLE_PODS = frozenset({"hdbc"})


def test_no_oracle_board_is_named_after_its_own_datacentre_pod():
    """Oracle Fusion tenants live at `<pod>.fa.<region>.oraclecloud.com`,
    where the pod is a four-letter code Oracle allocated. `Ecwr` and `Eizj`
    are not companies, and a reader cannot get from one to an employer."""
    bad = []
    for s in _sources():
        if s.get("platform") != "oracle":
            continue
        host = urlsplit(s["url"]).hostname or ""
        pod = re.sub(r"^fa-", "", host.split(".")[0])
        pod = re.sub(r"-(saasfaprod\d*|dev\d*|test\d*|stage\d*)$", "", pod)
        if pod in UNIDENTIFIED_ORACLE_PODS:
            continue
        if _norm(s.get("company")) == _norm(pod) and re.fullmatch(r"[a-z]{4}", pod):
            bad.append((s["company"], host))
    assert bad == [], bad


def test_no_workday_board_is_named_after_a_workday_url_word():
    """The tenant token is circular evidence for a board found by guessing
    the token, and the site id is worse: "External_Careers" names nobody."""
    bad = []
    for s in _sources():
        if s.get("platform") != "workday":
            continue
        m = re.search(r"/wday/cxs/([^/]+)/([^/]+)/jobs", s["url"])
        if not m:
            continue
        site = m.group(2)
        if _norm(s.get("company")) == _norm(site) and _norm(site) in {
                "external", "externalcareers", "careers", "search", "jobs",
                "internal", "externalcareersite"}:
            bad.append((s["company"], s["url"]))
    assert bad == [], bad


# Same platform, same name, genuinely different tenants -- and checked, one
# board at a time, against each board's own page title. Every group below is
# one employer running more than one portal (`careers-` and `jobs-`, hourly
# and corporate, a campus board beside the main one), which is not a defect:
# `careers-buffalojeans.icims.com` answers "Careers Center Job Listings at
# Centric Brands", and `careers-zenova.icims.com` answers "Job Listings at
# Wellpath". Listing them means a NEW collision still fails.
VERIFIED_MULTI_PORTAL = frozenset({
    ("icims", "ameritfleetsolutions"), ("icims", "bimcorporateheadoffice"),
    ("icims", "capreit"), ("icims", "centricbrands"),
    ("icims", "committeeforpubliccounselservices"), ("icims", "express"),
    ("icims", "fastenterprisesfastenterprises"),
    ("icims", "firebirdsinternationalllcfirebirdsrestaurants"),
    ("icims", "fruitoftheloomfotlinc"), ("icims", "goaheadlondongoaheadlondon"),
    ("icims", "libertymutual"), ("icims", "noodlescompany"), ("icims", "onemci"),
    ("icims", "redlobstermanagementllcredlobster"), ("icims", "spicerhaart"),
    ("icims", "springswindowfashions"), ("icims", "stjohnsriversidehospital"),
    ("icims", "trinseollc"), ("icims", "ustanationaltenniscenterinc"),
    ("icims", "vetcor"), ("icims", "waterscorporation"), ("icims", "wellpath"),
    ("oracle", "asterdmhealthcare"), ("oracle", "dtu"), ("oracle", "hilton"),
    ("oracle", "kent"), ("oracle", "milaha"), ("oracle", "ulsolutions"),
    ("greenhouse", "bgeinc"), ("greenhouse", "cobblestoneenergy"),
    ("greenhouse", "lts"), ("greenhouse", "supportingstrategies"),
    ("ashby", "arcadeai"), ("ashby", "glimpse"),
    ("recruitee", "heliumdoc"), ("workable", "humanintelligence"),
    ("workday", "rocket"),
})


def _tenant(s: dict) -> str:
    """The vendor's handle for this board, with the parts that mean "another
    portal for the same employer" removed: an iCIMS subdomain prefix, an
    Oracle dev pod suffix, a trailing digit on a second Greenhouse board."""
    u, plat = s["url"], s.get("platform")
    host = (urlsplit(u).hostname or "").lower()
    if plat == "icims":
        m = re.match(r"(?:[a-z0-9]+-)*?([a-z0-9]+)\.icims\.com", host)
        return m.group(1) if m else host
    if plat == "oracle":
        lab = re.sub(r"^fa-", "", host.split(".")[0])
        return re.sub(r"-(saasfaprod\d*|dev\d*|test\d*|stage\d*)", "", lab)
    if plat == "workday":
        # Tenant only. One employer routinely runs several sites on one
        # tenant -- experienced beside campus, external beside internal --
        # and those are the same employer, not a collision.
        m = re.search(r"/wday/cxs/([^/]+)/", u)
        return m.group(1).lower() if m else host
    for rx in (r"/boards/([^/]+)/", r"/job-board/([^?]+)", r"/companies/([^/]+)/",
               r"/accounts/([^/?]+)", r"/postings/([^?]+)",
               r"jobvite\.com/([^/]+)/"):
        m = re.search(rx, u)
        if m:
            return re.sub(r"[^a-z0-9]", "", m.group(1).lower())
    return host


def test_two_boards_on_one_platform_never_share_a_name_across_tenants():
    """Same name, same platform, unrelated tenants means one of them is
    mislabelled, and the dedupe rule that prefers a direct board over an
    aggregator has no way to tell which employer it is keeping."""
    groups: dict = {}
    for s in _sources():
        groups.setdefault((s.get("platform"), _norm(s.get("company"))), []).append(s)
    clashes = []
    for (plat, name), rows in groups.items():
        if len(rows) < 2 or (plat, name) in VERIFIED_MULTI_PORTAL:
            continue
        tenants = {_tenant(r) for r in rows}
        # One tenant containing another is the same employer's second board.
        if any(all(a in b or b in a for b in tenants) for a in tenants):
            continue
        clashes.append((plat, rows[0]["company"], sorted(tenants)))
    assert clashes == [], clashes


def test_every_board_has_a_name_a_platform_and_a_url():
    bad = [s for s in _sources()
           if not (s.get("company") or "").strip() or not s.get("url")
           or not s.get("platform")]
    assert bad == [], bad


# --------------------------------------------------------------------------
# A handshake that never happened is not a dead board
# --------------------------------------------------------------------------
def _ssl_error(reason: str) -> requests.exceptions.SSLError:
    """The shape requests raises: its own SSLError wrapping urllib3's, which
    wraps `ssl.SSLError` with `.reason` set to the alert name."""
    inner = ssl.SSLError(1, f"[SSL: {reason}] {reason.lower()} (_ssl.c:1129)")
    inner.reason = reason
    return requests.exceptions.SSLError(inner)


def test_a_tls_protocol_version_alert_is_recognised_as_a_handshake_failure():
    assert handshake_failure(_ssl_error("TLSV1_ALERT_PROTOCOL_VERSION")) == \
        "TLSV1_ALERT_PROTOCOL_VERSION"
    assert handshake_failure(_ssl_error("SSLV3_ALERT_HANDSHAKE_FAILURE")) == \
        "SSLV3_ALERT_HANDSHAKE_FAILURE"
    assert handshake_failure(_ssl_error("NO_SHARED_CIPHER")) == "NO_SHARED_CIPHER"


def test_a_bad_certificate_is_not_treated_as_a_handshake_failure():
    """An expired or mis-issued certificate is a fact about the host, which a
    browser would refuse too. Only the version and cipher alerts are about
    the machine doing the asking."""
    assert handshake_failure(_ssl_error("CERTIFICATE_VERIFY_FAILED")) is None


def test_an_ordinary_timeout_is_not_a_handshake_failure():
    assert handshake_failure(requests.exceptions.ConnectTimeout("timed out")) is None
    assert handshake_failure(requests.exceptions.ConnectionError("refused")) is None


def test_the_alert_is_still_found_when_the_build_leaves_reason_unset():
    """LibreSSL fills `.reason` in; not every build does. The text is the
    fallback, because missing one deletes a live employer."""
    inner = ssl.SSLError("[SSL: WRONG_VERSION_NUMBER] wrong version number")
    assert handshake_failure(requests.exceptions.SSLError(inner)) == \
        "WRONG_VERSION_NUMBER"


class _FakeSession:
    """A session that raises, and counts how many times it was asked."""

    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def get(self, *a, **k):
        self.calls += 1
        raise self.exc

    def post(self, *a, **k):
        self.calls += 1
        raise self.exc


def test_a_handshake_failure_is_reported_as_transport_not_as_a_status():
    src = Source(company="Roke", platform="custom",
                 url="https://www.roke.co.uk/wp-json/wp/v2/job?per_page=100")
    s = _FakeSession(_ssl_error("TLSV1_ALERT_PROTOCOL_VERSION"))
    res = fetch_one(src, session=s, retries=2)
    assert res.transport == "TLSV1_ALERT_PROTOCOL_VERSION"
    assert res.status is None
    assert not res.ok


def test_a_handshake_failure_is_not_retried():
    """It is deterministic. Retrying costs three connections and two backoff
    sleeps per source, every scan, to reach the same alert."""
    src = Source(company="Roke", platform="custom",
                 url="https://www.roke.co.uk/wp-json/wp/v2/job?per_page=100")
    s = _FakeSession(_ssl_error("TLSV1_ALERT_PROTOCOL_VERSION"))
    fetch_one(src, session=s, retries=2)
    assert s.calls == 1, f"asked {s.calls} times for a deterministic failure"


def test_an_ordinary_connection_error_is_still_retried():
    src = Source(company="Roke", platform="custom", url="https://example.invalid/x")
    s = _FakeSession(requests.exceptions.ConnectionError("refused"))
    fetch_one(src, session=s, retries=1)
    assert s.calls == 2


def test_the_handshake_error_says_whose_fault_it_is():
    src = Source(company="Roke", platform="custom",
                 url="https://www.roke.co.uk/wp-json/wp/v2/job?per_page=100")
    s = _FakeSession(_ssl_error("TLSV1_ALERT_PROTOCOL_VERSION"))
    err = fetch_one(src, session=s, retries=0).error or ""
    assert "www.roke.co.uk" in err
    assert ssl_backend().split()[0] in err          # LibreSSL / OpenSSL
    assert "not evidence it is gone" in err
    assert err != "SSLError"


def test_a_handshake_failure_reads_as_unreachable_and_is_never_prunable(monkeypatch=None):
    src = Source(company="Roke", platform="custom",
                 url="https://www.roke.co.uk/wp-json/wp/v2/job?per_page=100")
    real = discover.count_jobs.__globals__  # not patched; we patch fetch_one below
    del real

    def fake_fetch_one(s, **kw):
        return Result(s, error="TLS handshake failed (TLSV1_ALERT_PROTOCOL_VERSION)",
                      transport="TLSV1_ALERT_PROTOCOL_VERSION")

    import jobradar.fetch as fetch_mod
    saved = fetch_mod.fetch_one
    fetch_mod.fetch_one = fake_fetch_one
    try:
        row = discover.validate_source(src)
    finally:
        fetch_mod.fetch_one = saved
    assert row["verdict"] == "unreachable"
    assert row["transport"] == "TLSV1_ALERT_PROTOCOL_VERSION"
    assert row["prunable"] is False
    assert discover.prunable(row) is False


def test_a_board_that_answered_with_nothing_is_the_only_prunable_verdict():
    assert discover.prunable_row_verdict("dead", []) is True
    assert discover.prunable_row_verdict("dead", ["TLSV1_ALERT_PROTOCOL_VERSION"]) is False
    assert discover.prunable_row_verdict("unreachable", []) is False
    assert discover.prunable_row_verdict("live", []) is False
    assert discover.prunable_row_verdict("mismatch", []) is False


def test_a_report_row_from_before_this_change_still_refuses_the_tls_case():
    """Old reports have no `prunable` key. The fallback must not default to
    True, because the default here is a deletion."""
    assert discover.prunable({"verdict": "dead"}) is True
    assert discover.prunable({"verdict": "unreachable"}) is False
    assert discover.prunable({"verdict": "dead",
                              "transport": "TLSV1_ALERT_PROTOCOL_VERSION"}) is False


def test_the_board_hidden_behind_the_handshake_is_a_real_one():
    """The point of all of the above. `www.roke.co.uk` answers 200 to curl on
    the same machine, and the payload is a live board, so anything that reads
    the handshake failure as death deletes a working employer."""
    src = Source(company="Roke", platform="custom",
                 url="https://www.roke.co.uk/wp-json/wp/v2/job?per_page=100")
    payload = json.loads((FIXTURES / "custom_wordpress_roke.json")
                         .read_text(encoding="utf-8"))
    jobs = adapters.parse(payload, src)
    assert len(jobs) == 3
    assert all(j.company == "Roke" for j in jobs)
    assert all(j.url.startswith("https://www.roke.co.uk/") for j in jobs)


def test_a_multi_page_fetcher_carries_the_handshake_fact_out_with_it():
    """Eight of the nine platforms rebuild a Result around the failing page.
    Dropping `transport` there would leave the fix working on `custom` and
    `greenhouse` only, which is where the roke case happens to live."""
    from jobradar import fetch as fetch_mod
    src = Source(company="Someone", platform="workday",
                 url="https://x.wd1.myworkdayjobs.com/wday/cxs/x/y/jobs")

    def fake(s, **kw):
        return Result(s, error="TLS handshake failed (WRONG_VERSION_NUMBER)",
                      transport="WRONG_VERSION_NUMBER")

    saved = fetch_mod.fetch_one
    fetch_mod.fetch_one = fake
    try:
        res = fetch_mod.fetch_workday(src, ["manager"], retries=0)
    finally:
        fetch_mod.fetch_one = saved
    assert res.transport == "WRONG_VERSION_NUMBER"


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    bad = 0
    for name, fn in fns:
        try:
            fn()
            print(f"  pass  {name}")
        except BaseException as e:                   # noqa: BLE001
            bad += 1
            print(f"  FAIL  {name}: {e}")
    print(f"{len(fns) - bad}/{len(fns)} passed")
    sys.exit(1 if bad else 0)
