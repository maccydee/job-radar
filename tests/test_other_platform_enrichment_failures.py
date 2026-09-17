"""The other per-platform fetchers had the same bug LinkedIn's `fetch()` had:
every failure -- a network error, a non-200, a 200 that parsed to nothing --
collapsed to a bare `""`, indistinguishable from a genuinely empty response.

Found running a real enrich pass on 17 Sept 2026: `enrich.candidates()`
offered 14 roles across six platforms (phenom 8, breezy 2, oracle 1, workday
1, avature 1, icims 1) and all 14 failed silently. Checked live the same day,
polite requests only (1/sec per host, stopping after two failures), one at a
time:

  * Breezy -- two live tenants (the-engineering-society-of-queen-s-university,
    robust-open-online-safety-tools) serve ZERO application/ld+json blocks of
    any type on a healthy 200, so the shared JSON-LD reader this platform used
    to rely on entirely returned "" on both. One of the two postings is also
    closed ("no longer accepting candidates"); the other is live and has a
    server-rendered `<div class="description">` neither existing reader ever
    looked at.
  * Oracle -- requisition 110173 on efhi.fa.em3.oraclecloud.com answers its
    own REST API with `items: []` and is absent from the same site's current
    listing: a removed posting, not a broken request.
  * Workday -- the CXS job-detail endpoint answered every tenant tried
    (IQVIA, RBC, the Just Eat Takeaway myworkdaysite.com board) with HTTP 403
    "permission denied", including a tenant (IQVIA) enriched successfully on
    an earlier scan. Recorded as a blocked fetch; not worked around. Separately,
    and independent of the block, the myworkdaysite.com white-label URL shape
    was never parsed at all: `_workday_api` returned "" for it before any
    request was made.
  * Avature -- baufest.avature.net's JobDetail page has moved to a newer
    client-rendered `portalpacks` bundle with no JSON-LD, no field-value div
    and no description microdata; none of the three existing readers finds
    anything on a healthy 200.
  * iCIMS -- stopped putting a JobPosting node in its schema.org blocks at
    all: a Vista Global posting's two blocks nest Organization/WebSite/
    WebPage/BreadcrumbList and WebPage/Organization/LocalBusiness/
    BreadcrumbList under "@graph", never a JobPosting, so the shared reader
    finds nothing on any tenant now, not only the ones it never worked on.
  * "phenom" (Taleo, most of the 8) -- six of Mace's `stgmacecareers`
    postings answer 200 with Taleo's own `requisitionUnavailableInterface`
    filled in instead of the description one: removed requisitions, all six
    absent from the tenant's own live search the same day. The seventh, an
    Edmonton Transit posting, was stored as a `jobapply.ftl` link, which
    renders no description panel at all; the identical `job=` id at
    `jobdetail.ftl` carries 3,300 characters.

Every fixture below with an HTML comment at the top is real bytes off the
wire that day, trimmed to the part each test needs. Nothing in here touches
the network; `run()`'s counting, printing and flag-relabelling machinery is
exercised the same way `tests/test_linkedin_enrichment_failures.py` exercises
it for LinkedIn, because that machinery is already platform-agnostic and this
is what proves it stays that way for a fetcher it did not originally ship
with.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from jobradar import cli, enrich, store
from jobradar.config import Config

FIXTURES = Path(__file__).parent / "fixtures"


class _Response:
    def __init__(self, status, text="", payload=None):
        self.status_code = status
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _Session:
    """A `requests.Session` stand-in keyed by exact URL.

    `answers[url]` is `(status, text)`, `(status, None, payload)` for a JSON
    body, or the literal string `"raise"` for a network error. Anything not
    listed answers 404, the same default `FakeSession` in `tests/test_enrich.py`
    uses, so a fetcher asking for the wrong URL fails loudly instead of
    quietly finding nothing.
    """

    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def get(self, url, headers=None, timeout=None):
        self.asked.append(url)
        ans = self.answers.get(url)
        if ans == "raise":
            raise requests.ConnectionError("Connection reset by peer")
        if ans is None:
            return _Response(404, "")
        if len(ans) == 3:
            status, text, payload = ans
            return _Response(status, text or "", payload)
        status, text = ans
        return _Response(status, text)


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# --------------------------------------------------------------- Breezy
BREEZY_DESCRIPTION_DIV = _fixture("breezy_description_div.html")

# The exact copy a closed Breezy posting answers with, real bytes trimmed to
# the one div that carries it (see the-engineering-society-of-queen-s-
# university fixture captured 17 Sept 2026 for the untrimmed page).
BREEZY_CLOSED = (
    '<html><body><div class="confirmation-container">'
    '<div class="application-confirmed"><i class="fa fa-frown-o"></i>'
    '<h1>Position Closed</h1>'
    '<p>Sorry, this position is no longer accepting candidates.</p>'
    '</div></div></body></html>')


def test_a_breezy_tenant_with_no_json_ld_is_read_from_its_own_description_div():
    url = "https://robust-open-online-safety-tools.breezy.hr/p/7be1b76b64d0-director-of-engineering"
    assert not enrich._LD_BLOCK.findall(BREEZY_DESCRIPTION_DIV), \
        "the fixture must be a tenant that serves no JSON-LD at all"
    session = _Session({url: (200, BREEZY_DESCRIPTION_DIV)})

    text = enrich._from_breezy(url, session)
    assert not text.error, text.error
    assert text.startswith("Position Overview"), text[:60]
    assert "ROOST" in text
    assert "<p>" not in text, "markup is stripped, not handed to the scorer"


def test_a_closed_breezy_posting_is_reported_as_closed_not_a_parser_miss():
    url = "https://the-engineering-society-of-queen-s-university.breezy.hr/p/151318937cd7-x"
    session = _Session({url: (200, BREEZY_CLOSED)})

    text = enrich._from_breezy(url, session)
    assert text == ""
    assert "posting closed" in text.error, text.error
    assert "no longer accepting candidates" in text.error


def test_a_breezy_page_that_changed_shape_is_reported_generically():
    """Neither the JSON-LD block, the description div, nor the closed notice:
    a genuine shape the reader has never seen, and it must say so rather than
    look like either of the other two."""
    url = "https://somewhere.breezy.hr/p/abc-a-job"
    session = _Session({url: (200, "<html><body>hello</body></html>")})

    text = enrich._from_breezy(url, session)
    assert text == ""
    assert "no description div" in text.error, text.error


def test_a_breezy_network_failure_is_named_by_type():
    url = "https://x.breezy.hr/p/y-z"
    session = _Session({url: "raise"})
    text = enrich._from_breezy(url, session)
    assert "ConnectionError" in text.error, text.error


def test_a_breezy_failure_is_counted_reported_and_relabels_the_role():
    """The whole point, end to end, for a platform other than LinkedIn: a
    failed fetch is counted by (platform, reason), printed in `run()`'s
    notes, and the stale "no description" flag on the role is corrected to
    say what actually happened this run."""
    con = store.connect(Path(tempfile.mkdtemp()) / "t.db")
    url = "https://the-engineering-society-of-queen-s-university.breezy.hr/p/151318937cd7-x"
    con.execute(
        "INSERT INTO roles (uid,company,title,url,location,platform,"
        "description,flags,first_seen,last_seen) VALUES "
        "('b1','EngSoc','Braking Manager',?,'Ontario','breezy','',"
        "'[\"listing-only: no description available from this source\"]',"
        "'2026-09-17','2026-09-17')", (url,))
    con.execute("INSERT INTO role_state (uid,status,updated_at) "
                "VALUES ('b1','new','2026-09-17')")
    con.commit()

    real = requests.Session
    requests.Session = lambda: _Session({url: (200, BREEZY_CLOSED)})
    try:
        notes = []
        got, tried = enrich.run(con, Config(), pause=0, concurrency=1,
                                notes=notes)
    finally:
        requests.Session = real

    assert (got, tried) == (0, 1)
    assert any("breezy" in n and "posting closed" in n for n in notes), notes

    row = con.execute("SELECT flags FROM roles WHERE uid='b1'").fetchone()
    flags = json.loads(row["flags"])
    assert not any("listing-only" in f for f in flags), flags
    assert any("could not fetch" in f and "posting closed" in f
              for f in flags), flags


# --------------------------------------------------------------- Oracle
def test_oracle_empty_items_reports_the_requisition_as_removed():
    """A 200 with `items: []` is Oracle's own answer for a requisition ID
    that is no longer on the board (checked live, 17 Sept 2026: id 110173 on
    efhi.fa.em3.oraclecloud.com), not a malformed request. It must be told
    apart from a genuinely broken response."""
    url = ("https://efhi.fa.em3.oraclecloud.com/hcmUI/CandidateExperience/"
           "en/sites/CX_1/job/110173")
    api = ("https://efhi.fa.em3.oraclecloud.com/hcmRestApi/resources/latest/"
           "recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
           "&finder=ById;Id=%22110173%22,siteNumber=CX_1")
    session = _Session({api: (200, None, {"items": []})})

    text = enrich._from_oracle(url, session)
    assert text == ""
    assert "removed" in text.error, text.error


def test_an_oracle_404_is_still_reported_as_an_http_error():
    url = ("https://efhi.fa.em3.oraclecloud.com/hcmUI/CandidateExperience/"
           "en/sites/CX_1/job/110173")
    session = _Session({})  # nothing configured -> every request 404s
    text = enrich._from_oracle(url, session)
    assert "HTTP 404" in text.error, text.error


# --------------------------------------------------------------- Avature
AVATURE_PORTALPACKS = _fixture("avature_portalpacks.html")


def test_an_avature_client_rendered_tenant_is_reported_not_silently_empty():
    url = "https://baufest.avature.net/jobs/JobDetail/Mexico-Engineering-Manager/5886"
    assert not enrich._LD_BLOCK.findall(AVATURE_PORTALPACKS)
    assert not enrich._AV_FIELD.findall(AVATURE_PORTALPACKS)
    assert not enrich._AV_MICRO.findall(AVATURE_PORTALPACKS)
    session = _Session({url: (200, AVATURE_PORTALPACKS)})

    text = enrich._from_avature(url, session)
    assert text == ""
    assert "client-rendered template" in text.error, text.error


# --------------------------------------------------------------- iCIMS
ICIMS_EXPANDABLE_TEXT = _fixture("icims_expandable_text.html")
ICIMS_GRAPH_NO_JOBPOSTING = _fixture("icims_graph_no_jobposting.html")


def test_icims_no_longer_publishes_jobposting_json_ld_but_the_html_still_has_it():
    """`_json_ld_text` alone used to be the whole of `_from_icims`. It now
    finds nothing on a real posting (both schema.org blocks are
    Organization/WebSite/WebPage/BreadcrumbList-shaped, never JobPosting),
    and the fallback below has to recover the same advert from
    `iCIMS_Expandable_Text`, iCIMS' own template markup."""
    base = "https://careers-vistaglobal.icims.com/jobs/6082/engineering-manager/job"
    session = _Session({base + "?in_iframe=1": (200, ICIMS_EXPANDABLE_TEXT)})

    assert enrich._json_ld_text(ICIMS_EXPANDABLE_TEXT) == ""

    text = enrich._from_icims(base, session)
    assert not text.error, text.error
    assert "Vista is the world" in text
    assert "10+ years of experience" in text
    assert "<p>" not in text


def test_json_ld_text_does_not_mistake_a_graph_wrapped_organization_for_a_jobposting():
    """Real bytes: two ld+json blocks off the same posting's bare (non-iframe)
    URL, each an "@graph" of Organization/WebPage/BreadcrumbList-shaped nodes.
    A reader that only checked the top-level object's own "@type" would see
    neither ever set it, which is correct -- but a naive graph-unwrap that
    took the FIRST node regardless of type would misread one of these as the
    posting. It must still return nothing."""
    assert enrich._json_ld_text(ICIMS_GRAPH_NO_JOBPOSTING) == ""


def test_json_ld_text_finds_a_jobposting_nested_inside_at_graph():
    """The positive case the fix exists for: a JobPosting node IS in the
    bundle, just under "@graph" rather than at the top level or as a bare
    object, the shape iCIMS' own posting pages would use if they ever put
    the advert back."""
    page = ('<script type="application/ld+json">'
            '{"@context":"https://schema.org","@graph":['
            '{"@type":"Organization","name":"Acme"},'
            '{"@type":"JobPosting","description":"<p>Graph-nested advert.</p>"}'
            ']}</script>')
    assert enrich._json_ld_text(page) == "Graph-nested advert."


def test_an_icims_url_that_does_not_match_a_job_page_is_reported():
    text = enrich._from_icims("https://careers.icims.com/search", _Session({}))
    assert "doesn't match" in text.error, text.error


# --------------------------------------------------------------- Workday
def test_a_myworkdaysite_url_is_turned_into_its_tenants_cxs_endpoint():
    """The white-label domain reverses the usual order: myworkdayjobs.com
    puts the tenant in the subdomain, myworkdaysite.com puts the POD there
    instead and carries the tenant one path segment in, after "recruiting".
    Real URL, straight out of the 17 Sept 2026 database (a Phenom apply link
    for a Just Eat Takeaway requisition); `_workday_api` returned "" for it
    before this fix, matching neither pattern it knew."""
    url = ("https://wd3.myworkdaysite.com/recruiting/takeaway/JET-ECS-R/job/"
           "Fleet-Place-Office/Head-of-Engineering---Data-Platforms_R_052064-1"
           "/apply")
    assert enrich._workday_api(url) == (
        "https://takeaway.wd3.myworkdayjobs.com/wday/cxs/takeaway/JET-ECS-R/"
        "job/Fleet-Place-Office/Head-of-Engineering---Data-Platforms_R_052064-1")


def test_a_second_myworkdaysite_tenant_resolves_the_same_way():
    url = ("https://wd1.myworkdaysite.com/recruiting/tjx/TJX_EXTERNAL/job/"
           "CAN-Home-Office-Mississauga-ON/IT-Engineering-Manager_REQ128850-1"
           "/apply")
    assert enrich._workday_api(url) == (
        "https://tjx.wd1.myworkdayjobs.com/wday/cxs/tjx/TJX_EXTERNAL/job/"
        "CAN-Home-Office-Mississauga-ON/IT-Engineering-Manager_REQ128850-1")


# Real bytes: Workday CXS' own error body, captured live 17 Sept 2026 against
# three separate tenants (IQVIA, RBC, Just Eat Takeaway), IQVIA's included --
# a tenant a previous scan had enriched successfully, so this is Workday
# tightening the endpoint, not a URL this module gets wrong.
WORKDAY_403 = ('{"errorCode":"S22","errorCaseId":"D1A38EMU5HAEGQ",'
               '"httpStatus":403,"message":"permission denied","messageParams":{}}')


def test_a_blocked_workday_cxs_request_is_reported_not_silently_empty():
    url = ("https://iqvia.wd1.myworkdayjobs.com/en-US/IQVIA/job/"
           "Kirkland-Quebec-Canada/Product---Engineering-Lead_R1550246")
    api = ("https://iqvia.wd1.myworkdayjobs.com/wday/cxs/iqvia/IQVIA/job/"
           "Kirkland-Quebec-Canada/Product---Engineering-Lead_R1550246")
    session = _Session({api: (403, WORKDAY_403)})

    text = enrich._from_workday(url, session)
    assert text == ""
    assert text.error == "HTTP 403", text.error


def test_a_workday_url_with_no_cxs_shape_is_reported_rather_than_requested():
    session = _Session({})
    text = enrich._from_workday("https://example.com/not-workday", session)
    assert text == ""
    assert "doesn't match" in text.error, text.error
    assert session.asked == [], "a URL that cannot become a CXS call is never requested"


# --------------------------------------------------------------- Taleo
TALEO_UNAVAILABLE = _fixture("taleo_requisition_unavailable.html")


def test_a_removed_taleo_requisition_is_reported_as_removed_not_a_parser_miss():
    url = "https://stgmacecareers.taleo.net/careersection/ex/jobdetail.ftl?job=47233"
    session = _Session({url: (200, TALEO_UNAVAILABLE)})

    text = enrich._from_taleo(url, session)
    assert text == ""
    assert "posting removed" in text.error, text.error
    assert "no longer available" in text.error


def test_a_taleo_apply_link_is_normalized_to_the_detail_page_before_asking():
    """`jobapply.ftl` is the start of the application flow for the same
    requisition `jobdetail.ftl` describes and renders no description panel at
    all (measured live, 17 Sept 2026: 0 characters against 3,300 for the
    identical `job=` id). The URL is rewritten before the request is made, so
    only `jobdetail.ftl` is ever actually asked for."""
    given = "https://edmonton.taleo.net/careersection/2/jobapply.ftl?job=56005"
    want = "https://edmonton.taleo.net/careersection/2/jobdetail.ftl?job=56005"

    page = (
        "api.fillList('requisitionDescriptionInterface', 'descRequisition', ["
        "'56005','true','false','LRT Engineering Interface Manager',"
        "'!*!" + ("Manage the interface between systems. " * 8) + "',"
        "'Engineering','AB-Edmonton'"
        "]);"
    )
    session = _Session({want: (200, page)})

    text = enrich._from_taleo(given, session)
    assert session.asked == [want], session.asked
    assert not text.error, text.error
    assert text.startswith("Manage the interface")


if __name__ == "__main__":
    import traceback
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception:
                failed += 1
                print("FAIL", name)
                traceback.print_exc()
    sys.exit(1 if failed else 0)
