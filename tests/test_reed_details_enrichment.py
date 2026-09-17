"""Reed roles get their advert, pay period and contract type from the details endpoint.

The bug, as found on 17 Sept 2026: Reed's search endpoint returns a 453
character extract of each advert and a bare minimumSalary/maximumSalary with no
period. All 21 Reed roles in the real database had descriptions of 436 to 452
characters, so the dealbreakers ran against a fragment (two manufacturing
"Interim Engineering Manager" roles got through) and every day rate was stored
as a confirmed-nothing "year" figure. Nothing said so: a 450 character extract
clears the 200 character "barely screened" line, and the enrichment pass had
no fetcher for Reed at all.

Both fixtures are real payloads, trimmed: `reed_search_extracts.json` is two
rows from live searches and `reed_job_details.json` is the details endpoint's
answer for four job ids, two of them with their advert HTML cut short at a
paragraph end. The one em-dash in the originals was replaced with a comma.
No request in here reaches the network: the session is a stub.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from jobradar import enrich, screen, store
from jobradar.adapters.platforms import parse_reed
from jobradar.config import Config, Dealbreaker
from jobradar.models import Source

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH = json.loads((FIXTURES / "reed_search_extracts.json")
                    .read_text(encoding="utf-8"))
DETAILS = json.loads((FIXTURES / "reed_job_details.json")
                     .read_text(encoding="utf-8"))

SF = "57332211"        # SF Partners, per day, Temporary
PERM = "57300205"      # Engineering Manager, per annum, Permanent
HOURLY = "57305550"    # per hour, Temporary
HIDDEN = "57352594"    # salaryType per annum with no figures, Temporary

# Never a real key. The tests assert it is SENT, so it has to be something.
FAKE_KEY = "not-a-real-reed-key"

SRC = Source(company="Reed", platform="reed", country="UK",
             url="https://www.reed.co.uk/api/1.0/search?keywords=engineering")


class _Response:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


class _Session:
    """Stands in for requests.Session. Answers from the fixtures, or with a
    fixed status, and records every call so a test can count them."""

    calls: list = []
    status = 200

    def get(self, url, auth=None, headers=None, timeout=None):
        _Session.calls.append({"url": url, "auth": auth})
        if _Session.status != 200:
            return _Response(_Session.status, {"error": "stubbed"})
        job_id = url.rstrip("/").rsplit("/", 1)[-1]
        return _Response(200, DETAILS[job_id])

    def close(self):
        pass


def _db():
    con = store.connect(Path(tempfile.mkdtemp()) / "t.db")
    jobs = []
    for job in parse_reed(SEARCH, SRC):
        # The two steps a scan runs on every Reed posting that passed the
        # title gate, so the stored flags are the ones a scan would store.
        screen.enrich(job)
        screen.screen(job, Config())
        jobs.append(job)
    store.upsert_roles(con, jobs)
    return con


def _row(con, job_id):
    return con.execute("SELECT * FROM roles WHERE url LIKE ?",
                       (f"%/{job_id}",)).fetchone()


def _enrich(con, cfg, status=200):
    """Run the real enrichment pass with the network stubbed out."""
    _Session.calls, _Session.status = [], status
    real = requests.Session
    requests.Session = _Session
    try:
        notes: list = []
        got, tried = enrich.run(con, cfg, pause=0, concurrency=1, notes=notes)
    finally:
        requests.Session = real
    return got, tried, notes


def _rescreen(con, cfg):
    from jobradar import cli
    return cli._rescreen(con, cfg)


def test_the_fixture_extracts_are_what_the_search_really_returns():
    # If this fails the fixture was replaced with something that is not an
    # extract, and every test below would be proving nothing.
    for r in SEARCH["results"]:
        assert r["jobDescription"].endswith("... "), r["jobId"]
        assert "salaryType" not in r and "contractType" not in r


def test_a_stored_reed_extract_is_queued_for_its_details_and_flagged():
    con = _db()
    queued = {r["url"].rsplit("/", 1)[-1] for r in enrich.candidates(con)}
    assert {SF, PERM} <= queued, queued

    sf = _row(con, SF)
    assert 400 < len(sf["description"]) <= screen.REED_SNIPPET_MAX
    flags = json.loads(sf["flags"])
    assert any("barely screened" in f and "Reed" in f for f in flags), flags


def test_the_details_endpoint_replaces_the_extract_with_the_whole_advert():
    con = _db()
    got, tried, notes = _enrich(con, Config(reed_api_key=FAKE_KEY))
    assert (got, tried, notes) == (2, 2, [])

    sf = _row(con, SF)
    full = enrich._strip(DETAILS[SF]["jobDescription"])
    assert len(full) > 3000
    assert sf["description"] == full
    # From well past the 453 character extract.
    assert "Experience with Claude Code, GitHub Copilot" in sf["description"]
    assert ("This is not an AI strategy role, a research role or traditional "
            "change management.") in sf["description"]

    # Asked of the details endpoint, by id, with the key as the Basic
    # username and nothing on the session for other hosts to inherit.
    urls = {c["url"] for c in _Session.calls}
    assert f"https://www.reed.co.uk/api/1.0/jobs/{SF}" in urls
    assert all(c["auth"] == (FAKE_KEY, "") for c in _Session.calls)

    # And it is no longer an extract, so the next scan does not ask again.
    assert SF not in {r["url"].rsplit("/", 1)[-1]
                      for r in enrich.candidates(con)}


def test_a_reed_day_rate_is_stored_as_a_confirmed_day_rate():
    con = _db()
    before = _row(con, SF)
    assert before["salary_confirmed"] == 0, "the bug this fixes"

    _enrich(con, Config(reed_api_key=FAKE_KEY))
    sf = _row(con, SF)
    assert (sf["salary_min"], sf["salary_max"]) == (700.0, 1250.0), \
        "Reed's yearly 182,000 to 325,000 must not be what is stored"
    assert sf["salary_period"] == "day"
    assert sf["salary_confirmed"] == 1
    assert sf["salary_currency"] == "GBP"


def test_a_reed_contract_type_sets_employment():
    from jobradar import employment
    con = _db()
    # SF Partners' real title says "Contract (outside IR35)", which would
    # decide this on its own and prove nothing about contractType. So the
    # stored title is made neutral and the extract's verdict cleared.
    con.execute("UPDATE roles SET title='AI Enablement Lead', "
                "employment='unstated' WHERE url LIKE ?", (f"%/{SF}",))
    assert employment.classify("AI Enablement Lead")[0] == "unstated"
    _enrich(con, Config(reed_api_key=FAKE_KEY))
    assert _row(con, SF)["employment"] == "contract"

    # And with no advert text to fall back on, contractType alone decides.
    assert enrich._employment({"title": "AI Enablement Lead"},
                              "contract", "") == "contract"


def test_a_reed_per_annum_permanent_role_is_a_permanent_year_salary():
    con = _db()
    _enrich(con, Config(reed_api_key=FAKE_KEY))
    perm = _row(con, PERM)
    assert (perm["salary_min"], perm["salary_max"]) == (75000.0, 100000.0)
    assert perm["salary_period"] == "year" and perm["salary_confirmed"] == 1
    assert perm["employment"] == "permanent"
    # From contractType "Permanent", not from the words: the title and the
    # whole advert say nothing the text classifier accepts.
    from jobradar import employment
    assert employment.classify(perm["title"], perm["description"])[0] \
        == "unstated"
    assert len(perm["description"]) > 2000


def test_hourly_and_hidden_reed_salaries_from_the_details_payload():
    hourly = enrich.reed_details(DETAILS[HOURLY])
    assert (hourly.salary.min, hourly.salary.max) == (45.0, 50.0)
    assert hourly.salary.period == "hour" and hourly.salary.confirmed
    assert hourly.employment == "contract"

    # salaryType is still "per annum" when the employer hides the figure.
    # No figure is not a figure of zero: unconfirmed, so it can never
    # disqualify the role.
    hidden = enrich.reed_details(DETAILS[HIDDEN])
    assert hidden.salary.confirmed is False
    assert hidden.salary.min is None and hidden.salary.max is None
    assert hidden.employment == "contract", "a 12 month fixed term is Temporary"
    assert len(hidden) > 200


def test_a_contract_title_is_not_overwritten_by_a_platform_permanent():
    row = {"title": "Interim Engineering Manager", "platform": "reed"}
    assert enrich._employment(row, "permanent", "") == "contract"
    # And with no title to check, nothing is written rather than the
    # platform value being applied blind.
    assert enrich._employment({"platform": "reed"}, "permanent", "") == ""
    # A value already stored, perhaps from a platform field on the scan, is
    # not replaced by a guess at the prose.
    assert enrich._employment(
        {"title": "Engineering Manager", "employment": "permanent"}, "",
        "a six month contract position") == ""


def test_a_failed_details_fetch_leaves_the_role_flagged_as_an_extract():
    con = _db()
    before = _row(con, SF)["description"]
    got, _tried, notes = _enrich(con, Config(reed_api_key=FAKE_KEY), status=500)
    assert got == 0
    assert any("HTTP 500" in n for n in notes), notes

    sf = _row(con, SF)
    assert sf["description"] == before
    assert sf["salary_confirmed"] == 0
    # `_rescreen` rewrites flags after every enrichment pass, and it strips
    # "barely screened" flags from anything 200 characters or longer. The
    # extract must come straight back, or a failed fetch reads as a success.
    _rescreen(con, Config(reed_api_key=FAKE_KEY))
    flags = json.loads(_row(con, SF)["flags"])
    assert any("barely screened" in f and "extract" in f for f in flags), flags
    assert SF in {r["url"].rsplit("/", 1)[-1] for r in enrich.candidates(con)}


def test_a_refused_reed_key_is_reported_and_not_asked_again():
    con = _db()
    got, tried, notes = _enrich(con, Config(reed_api_key=FAKE_KEY), status=401)
    assert (got, tried) == (0, 2)
    assert len(_Session.calls) == 1, "every later Reed role would 401 too"
    assert any("refused" in n and "401" in n for n in notes), notes
    # Not recorded as a role with no advert: the extract is still there and
    # the role is still queued for when the key is fixed.
    assert len(_row(con, SF)["description"]) > 400
    assert len(enrich.candidates(con)) == 2


def test_no_reed_key_sends_nothing_and_says_so():
    con = _db()
    got, _tried, notes = _enrich(con, Config(reed_api_key=""))
    assert got == 0 and _Session.calls == []
    assert any("no Reed API key" in n for n in notes), notes


def _status(con, job_id):
    uid = _row(con, job_id)["uid"]
    return con.execute("SELECT status FROM role_state WHERE uid=?",
                       (uid,)).fetchone()["status"]


def test_rescreen_applies_the_floor_to_the_details_day_rate():
    # Before enrichment the day rate is unconfirmed, so a 300,000 floor
    # cannot act on it. After, 1,250 a day is 275,000 a year by this tool's
    # own rule and the role fails the floor. The stored salary is what
    # `_rescreen` reads, so this is also the proof enrichment persisted it.
    cfg = Config(reed_api_key=FAKE_KEY, salary_floor=300000,
                 salary_currency="GBP")
    con = _db()
    _rescreen(con, cfg)
    assert _status(con, SF) == "new"

    _enrich(con, cfg)
    _rescreen(con, cfg)
    assert _status(con, SF) == "closed"


def test_rescreen_applies_dealbreakers_to_the_full_advert():
    # "Claude Code" is in the advert and not in the extract.
    cfg = Config(reed_api_key=FAKE_KEY,
                 dealbreakers=[Dealbreaker("claude", r"claude code")])
    con = _db()
    assert "claude code" not in _row(con, SF)["description"].lower()
    _rescreen(con, cfg)
    assert _status(con, SF) == "new"

    _enrich(con, cfg)
    _rescreen(con, cfg)
    assert _status(con, SF) == "closed"


def test_the_enrich_command_rescreens_what_it_filled_in():
    # `job-radar enrich` fetched the advert and stopped, so a role whose full
    # text matched a hard dealbreaker stayed on the board until a scan next
    # happened to re-screen, while the command said the roles "can now be
    # screened". The scan's own enrich step re-screened; this did not.
    import argparse
    import contextlib
    import io
    from jobradar import cli

    tmp = Path(tempfile.mkdtemp())
    db = tmp / "t.db"
    con = store.connect(db)
    jobs = []
    for job in parse_reed(SEARCH, SRC):
        screen.enrich(job)
        screen.screen(job, Config())
        jobs.append(job)
    store.upsert_roles(con, jobs)
    con.commit()
    con.close()

    cfg_path = tmp / "config.yaml"
    cfg_path.write_text(
        "titles:\n  include:\n    - engineering manager\n"
        "dealbreakers:\n  - name: claude\n    pattern: \"claude code\"\n"
        "    hard: true\n"
        f"sources:\n  use_bundled: false\n  reed_api_key: {FAKE_KEY}\n",
        encoding="utf-8")
    args = argparse.Namespace(config=str(cfg_path), db=str(db), limit=0,
                              pause=0.0, dry_run=False, concurrency=1)

    _Session.calls, _Session.status = [], 200
    real = requests.Session
    requests.Session = _Session
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            assert cli.cmd_enrich(args) == 0
    finally:
        requests.Session = real

    con = store.connect(db)
    try:
        assert len(_row(con, SF)["description"]) > 3000
        assert _status(con, SF) == "closed"
        assert _status(con, PERM) == "new"
    finally:
        con.close()


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
