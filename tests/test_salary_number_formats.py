"""Half of Europe writes sixty thousand as 60.000, and none of it was legible.

`_NUM` knew one thousands separator, the comma. So "€ 60.000 - € 75.000 per
jaar", "EUR 65.000" and every other Dutch or German advert came back
unconfirmed, which is the safe direction but is also most of that market
reporting no salary at all.

The rule for a dot, and it is the whole rule: it separates thousands only when
EXACTLY three digits follow it and no digit follows those. "60.000" is sixty
thousand, "60.00" is two digits and stays sixty, "1.5" is one digit and stays
one and a half. Nobody quotes pay to three decimal places, which is what gives
the three-digit case a single honest reading. The two ambiguous shapes are
tested here on purpose, because getting either of them wrong turns a number
into one a thousand times its size.

The month words came in with the number format rather than after it, and that
coupling is the point of the last test in this file. While "€ 4.500 bruto per
maand" was unreadable it was harmless. The moment it parses, a figure with no
month word the parser recognises is confirmed as 4,500 A YEAR, and a confirmed
4,500 is below every floor anybody sets, so teaching the number format on its
own would have converted an unconfirmed salary into a deleted role.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar.salary import parse_text  # noqa: E402


def test_a_dot_with_three_digits_after_it_separates_thousands():
    s = parse_text("Salaris: € 60.000 - € 75.000 per jaar")
    assert s.confirmed, "a euro range written the Dutch way was unreadable"
    assert (s.min, s.max) == (60000.0, 75000.0), s
    assert s.currency == "EUR" and s.period == "year"


def test_the_same_thing_with_the_code_instead_of_the_symbol():
    s = parse_text("EUR 65.000")
    assert s.confirmed and s.max == 65000.0 and s.currency == "EUR", s


def test_two_dots_is_a_million():
    s = parse_text("INR 1.200.000")
    assert s.confirmed and s.max == 1200000.0, s


def test_two_digits_after_the_dot_is_a_decimal_and_stays_one():
    """The near miss. Read as a separator, 60.00 would be six thousand, and an
    hourly rate of six thousand annualises to over ten million."""
    s = parse_text("$60.00 per hour")
    assert s.max == 60.0, s
    assert s.period == "hour"


def test_one_digit_after_the_dot_is_a_decimal_and_stays_one():
    s = parse_text("$12.5 per hour")
    assert s.max == 12.5, s


def test_the_k_suffix_still_multiplies_rather_than_separating():
    s = parse_text("Salary £1.5k per day")
    assert s.max == 1500.0 and s.period == "day", s


def test_a_comma_decimal_after_a_dot_separator():
    s = parse_text("Tarief € 1.500,50 per day")
    assert s.max == 1500.5 and s.period == "day", s


def test_the_anglo_spelling_is_untouched():
    for text, lo, hi in (("£60,000 - £70,000", 60000.0, 70000.0),
                         ("$120,000.50", 120000.5, 120000.5),
                         ("£120k - £150k", 120000.0, 150000.0)):
        s = parse_text(text)
        assert (s.min, s.max) == (lo, hi), f"{text} -> {s}"


def test_the_number_format_did_not_arrive_without_the_month_words():
    """Both halves, in one test, because shipping the first without the second
    would have been a worse bug than the one being fixed.

    `Salary` has no period to store "month" in, so a Dutch or German monthly
    figure is multiplied by twelve and stored as the year it adds up to,
    exactly as `_REED_ANNUALISE` and `from_pinpoint` already do for a weekly
    or monthly rate off a structured API. The alternative -- reading "€
    4.500 bruto per maand" as an unmultiplied 4,500 a year -- is a role paying
    54,000 hidden behind any floor at all, on a number the advert never
    stated as its annual salary.
    """
    year = parse_text("Salaris: € 60.000 - € 75.000 per jaar")
    assert year.confirmed and year.max == 75000.0, year

    for monthly, annual in (("Salaris: € 4.500 bruto per maand", 54000.0),
                            ("Gehalt: EUR 5.500 pro Monat", 66000.0),
                            ("EUR 5.500 monatlich", 66000.0)):
        s = parse_text(monthly)
        assert s.confirmed and s.period == "year" and s.max == annual, \
            f"{monthly} -> {s}"


def test_the_german_year_words_are_read_as_a_year():
    s = parse_text("Gehalt: EUR 66.000 pro Jahr")
    assert s.confirmed and s.period == "year" and s.max == 66000.0, s
