"""A row must never quote a salary that was stated for somewhere else.

Kraken publishes every role on its Ashby board twice: once for the United
States, with a band, and once for the twenty-one other countries it hires in,
with none. Company and title are byte-identical, so both merge passes
collapsed the pair onto the US posting, joined the other countries onto its
location line and left the band where it was. What came out was one row that
reads as a United Kingdom vacancy, links to the United States application, and
quotes a United States band.

Nothing failed. No column was empty, no adapter returned nothing, no request
was refused, and the row renders exactly like a correct one, which is this
repo's signature shape and the reason this file exists.

The payload in `tests/fixtures/ashby_kraken_two_countries.json` is the real
response from `api.ashbyhq.com/posting-api/job-board/kraken.com`, trimmed to
the two postings under test and to a short description. The compensation and
location fields are untouched, because they are the fields being asserted.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import screen, store
from jobradar.adapters.platforms import parse_ashby
from jobradar.models import Source

FIXTURE = (Path(__file__).parent / "fixtures"
           / "ashby_kraken_two_countries.json")
SRC = Source(
    company="Kraken",
    url="https://api.ashbyhq.com/posting-api/job-board/kraken.com"
        "?includeCompensation=true",
    platform="ashby",
    sector="technology",
)
US_ID = "95771a6e-726d-4ee0-9775-92fd30ae9250"
UK_ID = "54d595d9-61b9-4446-9713-1378d3714d36"


def _postings():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return list(parse_ashby(payload, SRC))


def _by_id(jobs):
    """The jobs keyed on the posting id in their own URL.

    Keyed on the URL rather than on the location, because which location ends
    up on which row is the thing under test and using it to find the row would
    assume the answer.
    """
    out = {}
    for j in jobs:
        out[j.url.rstrip("/").rsplit("/", 1)[-1]] = j
    return out


def test_the_board_really_does_publish_two_postings():
    """The premise. If Ashby ever collapses these into one posting the rest of
    this file is asserting something that cannot happen any more, and a test
    that cannot fail is worse than no test."""
    jobs = _postings()
    assert len(jobs) == 2, [j.location for j in jobs]
    assert len({j.url for j in jobs}) == 2, "two postings, two apply pages"
    assert len({j.uid for j in jobs}) == 2, "uid derives from the URL"
    assert len({j.title for j in jobs}) == 1, "identical titles is the trap"


def test_the_adapter_reads_each_posting_own_location_and_own_pay():
    """Proves the crossing is not the adapter's doing before blaming the merge.
    One posting states a band and is open in one country; the other states
    nothing and is open in several."""
    got = _by_id(_postings())
    us, uk = got[US_ID], got[UK_ID]

    assert us.location == "United States"
    assert us.salary.currency == "USD"
    assert us.salary.min == 110400.0
    assert us.salary.max == 220800.0

    assert uk.location.startswith("United Kingdom")
    assert "Brazil" in uk.location, "secondaryLocations are read"
    assert uk.salary.min is None and uk.salary.max is None, \
        "this posting states no band at all"
    assert uk.salary.confirmed is False


def test_two_postings_differing_by_country_stay_two_rows():
    """The bug. `dedupe` grouped on company plus title, picked the posting with
    a band because a stated salary outranks an absent one, then joined the
    other posting's twenty-one countries onto it."""
    out = screen.dedupe(_postings(), None)
    assert len(out) == 2, \
        [(j.location, j.salary.label()) for j in out]

    rows = _by_id(out)
    assert set(rows) == {US_ID, UK_ID}, "each posting keeps its own apply page"

    us, uk = rows[US_ID], rows[UK_ID]
    assert us.location == "United States"
    assert (us.salary.currency, us.salary.min, us.salary.max) == \
        ("USD", 110400.0, 220800.0)

    assert uk.location.startswith("United Kingdom")
    assert "United States" not in uk.location
    # The whole point. This row is the one a UK reader sees.
    assert uk.salary.min is None and uk.salary.max is None, \
        "a UK posting must not inherit a US band"
    assert uk.salary.currency is None
    assert uk.salary.confirmed is False


def test_no_row_carries_a_field_belonging_to_another_posting():
    """Stronger than checking the two rows individually: every surviving row
    has to match one INPUT posting on location, URL and pay together. A row
    assembled out of two postings fails this even if each field looks
    plausible on its own, which is exactly how the merged row read."""
    postings = _postings()
    truth = {(j.url, j.location,
              j.salary.currency, j.salary.min, j.salary.max)
             for j in postings}
    for j in screen.dedupe(postings, None):
        assert (j.url, j.location, j.salary.currency,
                j.salary.min, j.salary.max) in truth, \
            f"{j.location!r} with {j.salary.label()!r} is not any real posting"


def test_the_stored_row_pass_does_not_re_merge_them_either():
    """Scan-time dedupe is only half of it. `merge_duplicates` runs over rows
    already in the database at the end of every scan, and if only one pass is
    fixed the other puts the band straight back on the next run. The two
    disagreeing is the older bug the function's own docstring describes."""
    con = store.connect(":memory:")
    for j in _postings():
        con.execute(
            "INSERT INTO roles (uid,company,title,url,location,platform,"
            "description,salary_min,salary_max,salary_currency,salary_period,"
            "salary_confirmed,first_seen,last_seen) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'2026-09-22','2026-09-25')",
            (j.uid, j.company, j.title, j.url, j.location, j.platform,
             j.description or "", j.salary.min, j.salary.max,
             j.salary.currency, j.salary.period,
             1 if j.salary.confirmed else 0))

    assert store.merge_duplicates(con) == 0, \
        "postings on different terms in different countries are not duplicates"

    rows = {r["url"].rstrip("/").rsplit("/", 1)[-1]: r
            for r in con.execute("SELECT url,location,salary_min,salary_max,"
                                 "salary_currency FROM roles").fetchall()}
    assert set(rows) == {US_ID, UK_ID}
    assert rows[US_ID]["location"] == "United States"
    assert rows[US_ID]["salary_max"] == 220800.0
    assert rows[UK_ID]["salary_min"] is None, \
        "the UK row must still state no band after the stored-row pass"
    assert rows[UK_ID]["salary_max"] is None
    assert "United States" not in rows[UK_ID]["location"]


# --------------------------------------------------------------------------
# The other half of the fix: it must not undo the merge the merge is for.
#
# One posting per office is the common case and six rows for one job is worse
# than one, which is why company plus title is deliberately the grouping key.
# A fix that split every group would trade this bug for that one.
# --------------------------------------------------------------------------
def _job(url, location, cur=None, low=None, high=None, platform="greenhouse"):
    from jobradar.models import Job, Salary
    return Job(company="Acme", title="Engineering Manager", url=url,
               platform=platform, location=location, description="x" * 50,
               salary=Salary(min=low, max=high, currency=cur,
                             confirmed=cur is not None))


def test_offices_in_one_country_still_collapse_to_one_row():
    """Nobody states pay, so there is nothing to attribute and nothing to
    cross. This is the majority of groups, because most adverts state no
    salary, and it has to behave exactly as it did before."""
    out = screen.dedupe([_job("https://x/1", "London"),
                         _job("https://x/2", "Manchester"),
                         _job("https://x/3", "Leeds")], None)
    assert len(out) == 1, [j.location for j in out]
    parts = screen.location_parts(out[0].location)
    assert sorted(parts) == ["Leeds", "London", "Manchester"]
    assert "posted in 3 locations" in out[0].flags


def test_an_office_stating_nothing_folds_into_one_stating_a_band_at_home():
    """US pay transparency is per state, so a board routinely carries a band on
    one office and none on the next in the same country. The band is still
    stated for that country, so joining the offices does not move it
    anywhere it does not apply, and splitting here would be noise."""
    out = screen.dedupe([_job("https://x/1", "New York", "USD", 200000, 260000),
                         _job("https://x/2", "Austin")], None)
    assert len(out) == 1, [(j.location, j.salary.label()) for j in out]
    assert out[0].salary.max == 260000
    assert sorted(screen.location_parts(out[0].location)) == \
        ["Austin", "New York"]


def test_two_countries_quoting_the_same_band_still_collapse():
    """Saying the same thing about pay makes a posting a copy of the same terms
    wherever it is open, so there is nothing to cross. Without this half, a
    board that repeats one identical band per country would split on every
    country it hires in."""
    out = screen.dedupe([_job("https://x/1", "London", "GBP", 120000, 150000),
                         _job("https://x/2", "Dublin", "GBP", 120000, 150000)],
                        None)
    assert len(out) == 1, [(j.location, j.salary.label()) for j in out]
    assert out[0].salary.currency == "GBP"
    assert sorted(screen.location_parts(out[0].location)) == \
        ["Dublin", "London"]


def test_a_keyword_search_copy_still_folds_into_the_employers_own_board():
    """The case `dedupe` was written for. Wise's Risk API role arrived from
    LinkedIn and from the employer's own board; the aggregator copy carries no
    salary and the real posting does. Both are the same city, so the band is
    not moved anywhere, and the aggregator must not come back as a second
    row."""
    out = screen.dedupe([
        _job("https://linkedin/1", "London, England", platform="linkedin"),
        _job("https://x/2", "London", "GBP", 130000, 160000)], None)
    assert len(out) == 1, [(j.platform, j.location) for j in out]
    assert out[0].platform == "greenhouse", "the employer's own board wins"
    assert out[0].salary.max == 160000


def test_a_posting_naming_no_country_does_not_inherit_a_band():
    """Bare "Remote" resolves to no country at all. Reading unknown as "the
    same country" is how this repo has lost roles before: a value meaning "we
    cannot say" must never be read as an answer. An extra visible row is
    recoverable, a silently foreign band is not."""
    out = screen.dedupe([_job("https://x/1", "San Francisco", "USD", 210000, 250000),
                         _job("https://x/2", "Remote")], None)
    assert len(out) == 2, [(j.location, j.salary.label()) for j in out]
    remote = [j for j in out if j.location == "Remote"]
    assert len(remote) == 1
    assert remote[0].salary.min is None and remote[0].salary.max is None
