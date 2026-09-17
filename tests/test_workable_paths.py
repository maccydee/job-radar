"""Reading a Workable board somewhere other than apply.workable.com.

The 2,094 bundled Workable boards are 2,094 requests to one host, paced at
0.7 a second because faster earned a sixteen hour 429, and that is fifty
minutes -- the floor of the whole scan. Worse than slow: `fetch.PER_HOST_RPS`
records 41 of 419 boards (9.8%) refused inside a run already paced at 0.7.

What was looked for was a second address for the same board. What was found,
and what these tests pin, is `jobs.workable.com/api/v1/companies/<uuid>`: the
same postings, on the host the search already uses, which took forty requests
at 2.83 a second without a refusal while a scan was saturating apply at the
same moment.

What was NOT found is a way to address it from a board slug, and the tests for
that are here too, because the next person to have this idea should be able to
read what was already tried instead of spending the requests again.

The four routes that do not exist, all checked with real requests on
2026-08-27, so that nobody spends a Workable ban finding out:

  * A per-tenant host. `<tenant>.workable.com` is a 301 to
    `apply.workable.com/<tenant>/`, and the widget path on it is a 404. The
    one thing it does serve is `/spi/v3/jobs`, Workable's documented API,
    which answers 401 `invalid_token` without the employer's own key. So
    there is no second hostname to spread 2,094 requests across.
  * The employer's own domain. 50 Workable employers' careers pages were
    fetched and not one served its postings from its own host. Workable's
    integration is `cdn.workable.com/assets/embed.js`, which is JSONP from the
    reader's browser to `apply.workable.com/api/v1/widget/accounts/<id>`; the
    employer's page carries the loader and nothing else. 9 used that widget,
    4 linked out to the board once, and the rest showed no Workable in their
    HTML at all. Zero carried a job list.
  * A feed. `apply.workable.com/robots.txt` names no sitemap.
    `jobs.workable.com/sitemap.xml` is 2,158 search landing pages and no
    employer or posting. No RSS, XML or JSON feed was found on either.
  * The documented API. workable.readme.io says 10 requests per 10 seconds on
    an account token, and the token belongs to the employer. It is not a way
    to read anybody else's board.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import adapters
from jobradar.models import Source
from jobradar.screen import directness

FIXTURES = Path(__file__).resolve().parent / "fixtures"
COMPANY = FIXTURES / "workable_company.json"
WIDGET = FIXTURES / "workable_widget.json"


def _company_payload() -> dict:
    return json.loads(COMPANY.read_text(encoding="utf-8"))


def _company_src() -> Source:
    return Source(
        company="F And F Properties",
        platform="workable_company",
        url="https://jobs.workable.com/api/v1/companies/"
            "7a6cbd0d-9003-4dfe-aebb-3b324ecbc569",
    )


def test_the_company_endpoint_returns_the_whole_board():
    """Not a search hit or two for this employer: every published role.

    `totalSize` is the board's own count, and it is what makes this a
    replacement for the widget rather than a supplement to the search.
    """
    payload = _company_payload()
    jobs = adapters.parse(payload, _company_src())
    assert payload["totalSize"] == 3
    assert len(jobs) == 3
    assert all(j.url.startswith("https://jobs.workable.com/view/") for j in jobs)


def test_the_employer_is_spelled_the_way_workable_spells_it():
    """The bundled list names employers from their board slug, so `cqs`
    becomes "Cqs" where Workable has "CQS SA". Taking the payload's spelling
    is what lets a role found this way and the same role found through the
    search land in the same `dedupe` group instead of showing twice."""
    jobs = adapters.parse(_company_payload(), _company_src())
    assert {j.company for j in jobs} == {"F&F Properties"}
    assert not any(j.company == "F And F Properties" for j in jobs)


def test_every_role_arrives_with_its_advert():
    """The reason this is worth a request at all. A board read through the
    widget carries the description too; a route that did not would have traded
    fifty minutes of Workable for an enrichment fetch per role."""
    jobs = adapters.parse(_company_payload(), _company_src())
    assert all(len(j.description or "") > 500 for j in jobs)
    assert all(j.location for j in jobs)
    assert all(j.posted_at for j in jobs)


def test_an_on_site_role_is_not_reported_as_remote():
    """Workable's own flag is spelled `on_site`, with an underscore.

    Checked over 80 postings from the search, the day feed and a company
    board on 2026-08-27: every value was `remote`, `hybrid` or `on_site`, and
    "on-site" never appeared. The parser used to look only for the hyphen, so
    every on-site role fell through to guessing from its location text --
    which is the guess the flag exists to remove.
    """
    payload = _company_payload()
    assert {j["workplace"] for j in payload["jobs"]} == {"on_site"}
    jobs = adapters.parse(payload, _company_src())
    assert all(j.remote is False for j in jobs)


def test_a_role_that_is_not_published_is_not_shown():
    """A board serves published roles and nothing else, so this has never been
    observed. It is pinned because the cost of being wrong is a draft advert
    in front of a reader, and the check is one line."""
    payload = _company_payload()
    payload["jobs"][0]["state"] = "draft"
    jobs = adapters.parse(payload, _company_src())
    assert len(jobs) == 2
    assert "Sales Team Manager" not in {j.title for j in jobs}


def test_these_postings_lose_to_the_employers_own_board():
    """They say `workable_search`, deliberately.

    The link handed to the reader is a jobs.workable.com view page either way,
    so this has to score below apply.workable.com in `directness` for exactly
    the reason the search does. An unlisted platform name defaults to 2, which
    is the employer's-own-board score, and would let this beat the real board
    in `dedupe` on description length.
    """
    jobs = adapters.parse(_company_payload(), _company_src())
    assert {j.platform for j in jobs} == {"workable_search"}
    assert directness("workable_search") < directness("workable")


def test_the_company_endpoint_is_told_apart_from_the_search():
    """They are one path segment apart on the same host, and they take
    different payload shapes: the search repeats the employer inside every
    item, the board wraps the employer around the list."""
    company = adapters.detect(
        "https://jobs.workable.com/api/v1/companies/"
        "7a6cbd0d-9003-4dfe-aebb-3b324ecbc569")
    search = adapters.detect("https://jobs.workable.com/api/v1/jobs?query=x")
    assert company.name == "workable_company"
    assert search.name == "workable_search"


def test_the_board_is_addressed_by_uuid_and_a_slug_will_not_do():
    """Recorded so nobody spends the requests finding this out twice.

    `jobs.workable.com/api/v1/companies/<x>` answers "Company not found" for
    the board slug the bundled list holds (`panelmatic`) and for the numeric
    account id the embed widget uses (`578094`). It answers for the account
    UUID and for that UUID's short form. The UUID is not in the job payload,
    the single-job payload or the application-form payload, and searching the
    employer's name found 17 of a sample of 40 bundled employers -- one of
    which was a different company with a similar name.

    So `workable_company` ships with no `build`: there is no token to build it
    from. A source using it has to have been handed the UUID.
    """
    platform = adapters.by_name("workable_company")
    assert platform.build is None
    assert "UUID" in platform.note or "uuid" in platform.note


def _widget_payload() -> dict:
    return json.loads(WIDGET.read_text(encoding="utf-8"))


def test_the_two_paths_list_the_same_postings_for_the_same_employer():
    """The claim the whole idea rests on, on one employer, from the two
    responses saved side by side.

    The wider run is in the report and does not fit in a fixture: 25 employers
    read both ways on 2026-08-27, 1,058 postings through the widget and 1,025
    through the company endpoint. 21 of the 25 matched exactly and 23 of the
    25 lost nothing. The two that lost postings lost real ones -- SPD
    Technology's board has 61 and the company endpoint returns 25, and the 36
    it drops include "Senior Engineering Manager". So this proves the two
    paths agree, which is not the same as proving one can replace the other.
    """
    widget_src = Source(
        company="F And F Properties", platform="workable",
        url="https://apply.workable.com/api/v1/widget/accounts/712456")
    widget = adapters.parse(_widget_payload(), widget_src)
    company = adapters.parse(_company_payload(), _company_src())

    assert {j.title for j in widget} == {j.title for j in company}
    assert len(widget) == len(company) == 3


def test_only_one_of_the_two_paths_carries_the_advert():
    """The widget URL the bundled list uses has no `details=true` on it, and
    without it the response has no description on any posting -- 3,035 bytes
    for this employer against 28,751 with it. So a board read costs a request
    here and an enrichment fetch later, while the company endpoint arrives
    with the advert already on it. That is a second reason to prefer this
    route where it can be addressed at all, and it is measured rather than
    assumed."""
    widget_src = Source(
        company="F And F Properties", platform="workable",
        url="https://apply.workable.com/api/v1/widget/accounts/712456")
    widget = adapters.parse(_widget_payload(), widget_src)
    company = adapters.parse(_company_payload(), _company_src())

    assert all(not (j.description or "") for j in widget)
    assert all(len(j.description or "") > 500 for j in company)


def test_the_recent_sweep_is_capped_inside_the_host_budget_and_says_so():
    """This used to assert the opposite: that the sweep was walked to
    exhaustion, because a cap would silently drop the tail of a sweep whose
    job is completeness. 21,062 postings in a week is over a thousand pages.

    Both halves of that stopped being true. The cap is not silent: the pager
    marks the result cut off and the scan names it. And the walk was not
    complete in practice: every scan from 7 to 17 September 2026 ended with
    jobs.workable.com refusing for up to a day, taking the sweep and most of
    the keyword searches with it. A thousand pages is several times what
    Workable's infrastructure has been seen to allow in one run.

    So what is pinned is the budget arithmetic, read from the module rather
    than grepped out of the dispatcher, and the walk itself is exercised in
    tests/test_workable_search_budget.py.
    """
    from jobradar import fetch as fetch_mod

    cap = fetch_mod.WORKABLE_RECENT_MAX_PAGES
    budget = fetch_mod.HOST_REQUEST_BUDGET["jobs.workable.com"]
    assert 0 < cap <= budget // 2, (
        f"the sweep may take {cap} of a {budget} request budget, leaving the "
        f"reader's own keyword searches too little")


def test_the_recent_sweep_and_the_keyword_search_are_told_apart():
    from jobradar import adapters
    assert adapters.detect(
        "https://jobs.workable.com/api/v1/jobs?day_range=7").name == "workable_recent"
    assert adapters.detect(
        "https://jobs.workable.com/api/v1/jobs?query=x").name == "workable_search"
    # Same payload shape, so it needs no parser of its own.
    assert (adapters.by_name("workable_recent").parse
            is adapters.by_name("workable_search").parse)


def test_workable_says_on_site_with_an_underscore():
    """The parser tested for "on-site" and "onsite". Workable sends `on_site`,
    verified against live payloads across three of its endpoints, and "on-site"
    never appears. Every on-site role was falling through to guessing from the
    location text, which is the guess the flag exists to remove.
    """
    from jobradar import adapters
    from jobradar.models import Source

    src = Source(company="x", platform="workable_search",
                 url="https://jobs.workable.com/api/v1/jobs?query=x")
    payload = {"jobs": [
        {"title": "A", "url": "https://jobs.workable.com/view/1",
         "company": {"title": "Acme"}, "workplace": "on_site",
         "location": {"city": "London"}, "description": "x" * 300},
        {"title": "B", "url": "https://jobs.workable.com/view/2",
         "company": {"title": "Acme"}, "workplace": "remote",
         "location": {"city": "London"}, "description": "x" * 300},
    ]}
    by = {j.title: j for j in adapters.parse(payload, src)}
    assert by["A"].remote is False, "an on_site role was not read as on site"
    assert by["B"].remote is True
