import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import deadlines                # noqa: E402

TODAY = date(2026, 10, 5)


def _iso(text, today=TODAY):
    r = deadlines.closing_date(text, today)
    return r.iso if r else None


def test_long_form_date_with_weekday_and_ordinal():
    r = deadlines.closing_date(
        "Closing Date for Applications: Friday 23rd October 2026 (COB)", TODAY)
    assert r.iso == "2026-10-23" and "23rd October 2026" in r.evidence


def test_bt_style_numeric_range_is_day_first_because_23_cannot_be_a_month():
    assert _iso("Posting Period: 23/09/2026 – 09/10/2026") == "2026-10-09"


def test_m_and_s_style():
    assert _iso("Closing date: 17th April 2026") == "2026-04-17"


def test_no_year_takes_the_next_occurrence():
    assert _iso("Applications close on 9 October") == "2026-10-09"


def test_ambiguous_numeric_date_is_unknown_not_guessed():
    assert _iso("Closing date 04/05/2026") is None


def test_a_date_with_no_closing_cue_is_ignored():
    assert _iso("We were founded on 3 March 2014 and have grown since.") is None


def test_impossible_date_is_not_a_date():
    assert _iso("closes 31 February 2026") is None


def test_empty_and_none():
    assert _iso("") is None
    assert deadlines.closing_date(None, TODAY) is None


def test_a_weak_cue_does_not_outvote_a_clear_closing_date():
    """"until" also introduces contract end dates and notice periods, so the
    latest date near any cue would have moved this deadline to December."""
    text = ("Closing date: 17th April 2026. This is a fixed term contract "
            "running until 31 December 2026.")
    assert _iso(text) == "2026-04-17"


def test_a_weak_cue_alone_still_reads():
    assert _iso("Please apply by end of 23rd October 2026") == "2026-10-23"


def test_an_unreadable_date_near_a_cue_is_flagged_and_a_clean_text_is_not():
    assert deadlines.has_closing_cue_with_unreadable_date("Closing date 04/05/2026") is True
    assert deadlines.has_closing_cue_with_unreadable_date("Closing date: 17th April 2026") is False
    assert deadlines.has_closing_cue_with_unreadable_date("closes soon, apply now") is False
    assert deadlines.has_closing_cue_with_unreadable_date("") is False


# ------------------------------------------------ review finding 4: nearest date

T8 = date(2026, 10, 8)


def test_the_date_nearest_the_cue_wins_not_the_latest_in_the_window():
    assert _iso("Closing date: 23 October 2026. Interviews: 6 November 2026.", T8) == "2026-10-23"
    assert _iso("Closing date: 23rd October 2026 (interviews w/c 2 November)", T8) == "2026-10-23"
    assert _iso("Apply by 20 October 2026; start date 4 January 2027.", T8) == "2026-10-20"
    assert _iso("Applications close 31 October 2026. We are an equal opportunity employer "
                "since 1 May 2030.", T8) == "2026-10-31"


def test_an_explicit_range_still_closes_on_its_last_date():
    assert _iso("Posting Period: 23/09/2026 - 09/10/2026", T8) == "2026-10-09"
    assert _iso("Posting period: 1 October 2026 to 30 October 2026", T8) == "2026-10-30"


def test_close_as_an_ordinary_word_is_not_a_closing_cue():
    assert _iso("Join a close-knit team. Posted 1 September 2026.", T8) is None
    assert _iso("You will work in close partnership with the CFO. Start date: 5 January 2027.",
                T8) is None
    assert _iso("Deadline-driven environment. Founded 12 March 2027 is our next milestone.",
                T8) is None
    assert not deadlines.has_closing_cue_with_unreadable_date(
        "Join a close-knit team. Posted 1 September 2026.", T8)


def test_closes_followed_by_its_date_is_still_a_cue():
    assert _iso("This vacancy closes on 23 October 2026.", T8) == "2026-10-23"
    assert _iso("Vacancy closes: 23 October 2026", T8) == "2026-10-23"
    assert _iso("The advert closes 23 October 2026.", T8) == "2026-10-23"


def test_two_cues_that_name_different_dates_are_left_blank_and_counted():
    text = "Closing date: 23 October 2026. Deadline for references: 6 November 2026."
    assert _iso(text, T8) is None
    assert deadlines.has_closing_cue_with_unreadable_date(text, T8) is True


# --------------------------------------------- review finding 19: ISO dates

def test_an_iso_closing_date_is_read():
    assert _iso("Closing Date: 2026-10-23", T8) == "2026-10-23"
    assert _iso("Applications close on 2026-10-23.", T8) == "2026-10-23"


def test_an_iso_range_closes_on_its_last_date():
    assert _iso("Posting period: 2026-09-23 - 2026-10-09", T8) == "2026-10-09"


def test_an_impossible_iso_date_is_not_a_date_but_is_counted():
    assert _iso("Closing date: 2026-02-31", T8) is None
    assert deadlines.has_closing_cue_with_unreadable_date("Closing date: 2026-02-31", T8) is True
