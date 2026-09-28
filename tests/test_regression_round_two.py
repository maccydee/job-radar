"""Regressions introduced by this morning's fixes, found by re-running users.

Four of them. Each is a fix that worked on the case it was written for and
broke, or failed to reach, something beside it.

- `regions_in` was wired into `match` and nowhere else, so a role in "Remote
  - Europe" passed the filter and then scored twenty points BELOW a bare
  "Remote", which is scored as naming nowhere.
- The citizenship pattern let any three words sit between the verb and the
  noun, so "a good corporate citizen" read as a bar on working here.
- European decimals became readable without the month vocabulary that has to
  go with them, so "€8.000 im Monat" was confirmed as an annual 8,000 and
  deleted by the floor with a reason stating the opposite of the advert.
- The written dashboard read the board BEFORE the scan's own cleanup, so it
  showed roles the same run had closed and a merged duplicate twice.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar.config import Config          # noqa: E402
from jobradar.models import Job             # noqa: E402
from jobradar.salary import parse_text      # noqa: E402
from jobradar.screen import enrich, score, work_rights  # noqa: E402

BODY = "We are hiring a marketing manager to run our campaigns. " * 8


def _scored(location, countries=("GR",)):
    cfg = Config()
    cfg.titles_include = ["marketing manager"]
    cfg.countries = list(countries)
    job = Job(company="Acme", title="Marketing Manager", url="https://x/1",
              platform="ashby", location=location, description=BODY)
    return score(job, cfg), job


def test_a_region_that_contains_your_country_scores_like_your_country():
    europe, _ = _scored("Remote - Europe")
    athens, _ = _scored("Athens, Greece")
    nowhere, _ = _scored("Remote")
    assert europe == athens, (europe, athens)
    assert europe > nowhere, "the qualifier that helps still costs points"


def test_a_region_that_does_not_contain_your_country_does_not():
    namer, _ = _scored("Remote - NAMER")
    nowhere, _ = _scored("Remote")
    assert namer == nowhere, (namer, nowhere)


def test_the_country_facet_can_place_a_region():
    _, job = _scored("Remote - Europe")
    enrich(job)
    assert job.country == "multiple", job.country


def _rights(text):
    return work_rights(Job(company="Acme", title="Engineer", url="https://x/1",
                           platform="ashby", description=text))


def test_a_good_corporate_citizen_is_not_a_bar_on_working_here():
    for text in ("You must be a good corporate citizen and a team player.",
                 "You must be a self starter and a strong communicator.",
                 "We are comfortable working with nationals of many countries."):
        assert _rights(text) != "no sponsorship", text


def test_a_real_citizenship_requirement_still_is_one():
    for text in ("Applicants must be Singapore Citizens or Permanent Residents.",
                 "Applicants must be US citizens.",
                 "You must be a British citizen.",
                 "Applicants must be permanent residents."):
        assert _rights(text) == "no sponsorship", text


MONTHLY = (
    "salary: €8.000 im Monat", "salary: €8.000/maand",
    "€5.000 μηνιαίως",
    "€8.000 par mois", "€8.000 al mes", "€8.000 mensile",
    "€8.000/mo", "€8.000 p.m.", "$6k-$10k/mo OTE",
    "€8.000 per maand", "€8.000 per month",
)


def test_a_monthly_figure_is_annualised_not_read_as_the_year_figure():
    """The floor deletes on a confirmed figure. A monthly salary STORED
    as the bare monthly number would be a 96,000 job hidden with the reason
    "pay below floor", which states the opposite of the advert -- so
    `_scan` multiplies a monthly figure up by twelve rather than storing it
    unmultiplied. Flo Health's Greenhouse posting states "Salary Range -
    gross per month ... €9.000 - €11.000 EUR", and this codebase's own
    live data held it as a confirmed €9,000-€11,000 A YEAR before this fix,
    for the same reason: a monthly figure with nowhere to put "month" used
    to be thrown away entirely rather than annualised, and a second match on
    the trailing number alone then read it as an unlabelled annual one."""
    for t in MONTHLY:
        s = parse_text(t)
        assert s.confirmed and s.period == "year", (t, s)
        # 8 EUR-shaped adverts above state 8.000 a month; annualised that is
        # 96,000, not 8,000.
        if "8.000" in t or "8k" in t.lower():
            assert s.max == 96000.0, (t, s)


def test_an_annual_figure_still_confirms():
    for text in ("€90.000 per jaar", "£140,000 per annum",
                 "€90.000 - €110.000", "$150,000 a year"):
        assert parse_text(text).confirmed, text


def test_the_page_is_written_after_the_scans_own_cleanup():
    """It was written from a snapshot taken before `merge_duplicates` and the
    enrichment pass, so it showed roles that run had just closed and a merged
    duplicate twice, at a uid no longer in the database."""
    import inspect
    from jobradar import cli
    src = inspect.getsource(cli.cmd_scan)
    snapshot = src.index("board = kept if args.dry_run")
    for later in ("merge_duplicates", "_enrich_step"):
        assert src.index(later) < snapshot, \
            f"the board is read before {later} again"


def test_a_re_read_never_overwrites_a_salary_that_is_already_confirmed():
    """A guess must not beat a fact.

    `rescreen` and `seed load` re-read the pay out of the advert, because the
    parser improves and a stored figure is whatever the code understood on
    the day. But text that is not pay parses as pay often enough to matter:
    6 of 300 descriptions padded with `hex(randomblob(200))` come back as a
    CONFIRMED salary. Left unguarded, the re-read replaced a stored 20,000
    with a number out of random hex and the role then cleared a floor it
    fails. It failed on CI and passed here, which is what a 2% chance looks
    like.

    So the re-read only ever fills a gap. The cost is that a figure parsed
    wrongly by an older seed builder stays wrong until the weekly rebuild,
    which is a week of one bad number against a chance of inventing one.
    """
    import inspect
    from jobradar import cli
    for fn in (cli.cmd_seed_load, cli.cmd_rescreen):
        src = inspect.getsource(fn)
        i = src.find("parse_text(")
        assert i != -1, fn.__name__
        guard = src[max(0, i - 400):i]
        assert "not j.salary.confirmed" in guard, \
            f"{fn.__name__} re-reads pay over a confirmed figure again"


def test_random_text_really_can_parse_as_pay():
    """The premise of the guard above. If this ever stops being true the
    guard can be reconsidered; while it is true, it cannot."""
    import sqlite3
    from jobradar.salary import parse_text
    con = sqlite3.connect(":memory:")
    hits = 0
    for _ in range(300):
        blob = con.execute("SELECT 'An ordinary advert. ' || "
                           "hex(randomblob(200))").fetchone()[0]
        if parse_text(blob).confirmed:
            hits += 1
    assert hits > 0, "random hex no longer parses as a salary at all"
