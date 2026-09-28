"""Twelve roles, confirmed, under GBP 15,000 a year. Not low pay -- misparses.

Found live on 2026-09-27:

  * Smarkets "Head of Security Engineering": GBP 1,000, confirmed. The
    advert states no salary at all; the figure is Elliptic and Smarkets'
    own "Learning & Development: £1,000 annual education budget", a perk
    line the bonus-figure filter did not catch because "education" is not
    one of its leadwords. A confirmed salary is the only kind that can
    delete a role or clear a floor it does not deserve, so a benefits
    budget standing in for one is worse believed than ignored.
  * Directsupply "Senior Manager, Engineering": GBP 2,013 - GBP 2,026. The
    advert's footer reads "© 2013 to 2026 Direct Supply, Inc. All rights
    reserved.", and with "package" sitting in the sentence just before it
    ("Generous benefit package available.") that cleared the pay-context
    gate as a copyright notice read as a salary range.
  * Flo Health "Engineering Manager": EUR 9,000 - 12,000, confirmed as a
    YEAR's pay. The advert states "Salary Range - gross per month ...
    €9.000 - €11.000 EUR": `_period_near` reads "month" correctly from
    the words beside the range, but the old code threw a monthly range away
    rather than multiplying it up, and a second match on the lone trailing
    figure landed outside the 60-character window that would have told it
    "month" too, so it read as an unlabelled annual 11,000.

Three defences, all in `jobradar/salary.py`:

  1. `_implausible_annual` -- a plausibility floor. A figure below it is not
     a low salary, it is something else that looked like one.
  2. `_looks_like_year_range` -- two whole numbers that are each a plausible
     calendar year are a date, not pay.
  3. The "month" branch in `_scan` -- a stated monthly figure is multiplied
     by twelve and stored as the year it adds up to, not discarded.

All three convert a wrongly CONFIRMED figure into an honest `confirmed=False`
with the raw figure kept, never into a silent empty `Salary()`: a value
meaning "we cannot say" must never be read as "no".
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar.salary import (clears_floor, currency_of_country,  # noqa: E402
                            parse_text)


# --- The three live cases, reproduced from the fixtures they came from ----

SMARKETS = (
    "This is our inaugural security hire, working alongside Infrastructure. "
    "You will own our detection and response programme end to end. "
    "Benefits: - Free lunch every day, because great work starts with great "
    "food. - Cycle-to-Work Scheme: Support for sustainable commuting and "
    "staying active. - Learning & Development: £1,000 annual education "
    "budget for courses, conferences, books and other learning resources.")

DIRECTSUPPLY = (
    "In the Manager, Engineering position, you will lead a broad team of "
    "Partners focused on engineering, third party integrations, and "
    "platform creation. " + ("Detail. " * 60) +
    "Job to be performed in the location listed. Generous benefit package "
    "available.\n© 2013 to 2026 Direct Supply, Inc. All rights reserved.")

FLO_HEALTH = (
    "We are hiring an Engineering Manager to lead our Data Science team. "
    + ("Detail about the role and the team. " * 16) +
    "Salary Range - gross per month (ranges may vary based on skills and "
    "experience) €9.000 - €11.000 EUR How we work: we are a "
    "mission-led, product-driven team.")


def test_smarkets_education_budget_is_not_confirmed_as_the_salary():
    s = parse_text(SMARKETS, currency_of_country("UK"))
    assert not s.confirmed, s


def test_directsupply_copyright_years_are_not_confirmed_as_the_salary():
    s = parse_text(DIRECTSUPPLY, currency_of_country("US"))
    assert not s.confirmed, s


def test_flo_health_monthly_range_is_annualised_not_stored_as_the_year():
    s = parse_text(FLO_HEALTH, currency_of_country("multiple"))
    assert s.confirmed, s
    assert s.period == "year", s
    assert s.min == 108000.0 and s.max == 132000.0, s


# --- 1. The plausibility floor, and that it does not hardcode GBP --------

def test_a_tiny_figure_in_major_currencies_is_marked_unknown_not_zero():
    for cur in ("GBP", "USD", "EUR", "CAD", "AUD", "CHF"):
        s = parse_text(f"Salary: {cur} 1,000 per annum, negotiable.")
        assert not s.confirmed, (cur, s)
        # Recorded as unknown, never as zero and never silently dropped: the
        # figure the advert stated is kept on the unconfirmed Salary.
        assert s.min == 1000.0 and s.max == 1000.0, (cur, s)
        assert s.currency == cur, (cur, s)


def test_the_same_raw_number_is_plausible_in_a_large_notation_currency():
    """1,000 units is not a year's pay in GBP. 1,000,000 units very much can
    be in JPY, KRW, HUF, IDR, VND, CLP or ISK, because those currencies
    write ordinary salaries in much bigger raw numbers -- JPY and KRW have
    no meaningful fractional unit, and the others are quoted the same way.
    Applying GBP's floor to every currency would either accept a JPY
    misparse a GBP reader would catch, or (the direction this test checks)
    reject a perfectly ordinary JPY salary for being "too small" on a scale
    that was never JPY's scale to begin with.
    """
    for cur in ("JPY", "KRW", "HUF", "IDR", "VND", "CLP", "ISK"):
        s = parse_text(f"Annual salary: {cur} 3,000,000, negotiable on experience.")
        assert s.confirmed, (cur, s)


def test_the_large_notation_floor_still_catches_an_implausible_figure():
    """The scaled-up floor is not switched off for these currencies, it is
    scaled: a JPY figure that is implausible even by JPY's own much bigger
    numbers is still rejected."""
    s = parse_text("Annual salary: JPY 12,000, negotiable on experience.")
    assert not s.confirmed, s
    assert s.min == 12000.0 and s.currency == "JPY", s


def test_a_genuinely_low_but_real_salary_still_clears_a_low_floor():
    """The floor is a sanity check on the SIZE of the number, not a minimum
    wage rule: a role stating a real, low salary is still confirmed and can
    still clear a floor set below it."""
    s = parse_text("Salary: £18,000 per annum for this junior role.")
    assert s.confirmed, s
    assert clears_floor(s, 15000.0, "GBP") == (True, "")


# --- 2. Year-range rejection ----------------------------------------------

def test_a_close_pair_of_calendar_years_is_not_read_as_a_salary_range():
    for text in ("We offer a generous compensation package. Founded in "
                 "1998, incorporated 2013 to 2026 and growing fast.",
                 "Generous benefit package available. © 2015 - 2021 "
                 "Example Corp. All rights reserved."):
        s = parse_text(text)
        assert not s.confirmed, (text, s)


def test_a_real_salary_range_that_is_not_years_still_confirms():
    """The year-range rule only fires when BOTH numbers look like a
    calendar year. A genuine range nowhere near that window is untouched."""
    s = parse_text("Salary: £45,000 - £65,000 per annum")
    assert s.confirmed and s.min == 45000.0 and s.max == 65000.0, s


def test_a_range_with_only_one_year_shaped_number_still_confirms():
    """One number in the 1990-2035 window is not two, so an ordinary range
    that happens to start near a plausible salary a year-shaped number could
    be confused for is not touched by this rule alone -- it still has to
    clear the plausibility floor on its own merits, which a real salary
    range does."""
    s = parse_text("Salary: £2,020 - £55,000 per annum")
    assert s.confirmed and s.min == 2020.0 and s.max == 55000.0, s


# --- 3. Monthly-period annualisation --------------------------------------

def test_a_stated_monthly_figure_is_annualised():
    s = parse_text("We offer a salary of £4,500 per month, negotiable.")
    assert s.confirmed and s.period == "year", s
    assert s.min == 54000.0 and s.max == 54000.0, s


def test_a_stated_monthly_range_is_annualised():
    s = parse_text("Salary range: £5,000 - £6,000 per month, DOE.")
    assert s.confirmed and s.period == "year", s
    assert s.min == 60000.0 and s.max == 72000.0, s


def test_an_unstated_period_that_is_implausibly_low_is_marked_unknown_not_guessed():
    """The plausibility floor's job, not the month branch's. A figure this
    small with no period word anywhere near it is not multiplied by twelve
    on a guess -- guessing the wrong multiplier would be exactly the kind
    of confident wrong answer this file exists to avoid. It is marked
    unconfirmed instead, with the number kept."""
    s = parse_text("Compensation: £4,500, to be discussed at interview.")
    assert not s.confirmed, s
    assert s.min == 4500.0, s


def test_a_month_word_describing_something_else_is_not_believed():
    """Annualising on "month" was built to fix Flo Health, and it broke the
    Financial Conduct Authority's advert on the very first live sweep after
    it was written: "...expected to be in a minimum of 60% per month. The
    salary range for the role is London - £140,000 - £190,000..." states a
    real £140,000-£190,000 salary, but "per month" is 42 characters before
    the figure, well inside `_PERIOD_WINDOW`, and describes office
    attendance, not pay. Annualising there reads a real £190,000 role as
    £2,280,000, confirmed -- a wrong CONFIRMED number, worse than the
    figure it replaced, and the kind of failure that renders identically
    to a success unless it is checked for directly. The sentence boundary
    between "per month" and the figure is what tells them apart, and only
    the stretch actually carrying the "month" evidence is checked: an
    unrelated sentence break on the OTHER side of the figure -- Flo
    Health's own advert reads "...€11.000 EUR How we work We're a
    mission-led..." right after its figure -- must not suppress a real
    "month" reading that never crossed anything."""
    text = ("Our Executive Director and Directors will be expected to be "
            "in a minimum of 60% per month. The salary range for the role "
            "is London - £140,000 - £190,000 and National - "
            "£126,000 - £171,000.")
    s = parse_text(text)
    assert s.confirmed and s.period == "year", s
    assert s.min == 140000.0 and s.max == 190000.0, s
