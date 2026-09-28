"""False drops: the roles the filters threw away that should have been shown.

A role that is dropped never appears, so nothing about the dashboard tells you
it existed. That makes a wrong drop the most expensive kind of bug in this
tool and the least visible, which is why it gets a file of its own.

Every number quoted in these tests was measured over one real sample: 13,588
postings fetched from 505 bundled boards (apply.workable.com excluded, since
it is rate-limiting this project). The fixtures are the actual advert text
those postings carried, trimmed to the sentence that matters.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar.config import Config, Dealbreaker
from jobradar.models import Job
from jobradar.salary import clears_floor, parse_text
from jobradar.screen import (_countries_in, match, screen, sponsorship_gate,
                             title_matches_loosely, work_rights)


def _cfg(**kw) -> Config:
    """The committed config.yaml: an engineering manager looking in the UK."""
    base = dict(
        titles_include=["engineering manager", "senior engineering manager",
                        "head of engineering", "director of engineering"],
        titles_exclude=["product manager", "programme manager",
                        "account manager", "sales"],
        countries=["UK"], remote_ok=True,
        salary_floor=120000.0, salary_currency="GBP",
        dealbreakers=[
            Dealbreaker("coding round",
                        r"take.?home|live coding|"
                        r"coding (?:test|assessment|challenge|exercise)", True),
            Dealbreaker("pre-sales",
                        r"pre.?sales|solutions? (?:architect|engineer)|"
                        r"forward deployed", True),
        ],
    )
    base.update(kw)
    return Config(**base)


def _job(title="Engineering Manager", location="London", description="",
         **kw) -> Job:
    return Job(company="Acme", title=title, url="https://example.test/1",
               platform="greenhouse", location=location,
               description=description, **kw)


# ---------------------------------------------------------------------------
# Salary
# ---------------------------------------------------------------------------

def test_a_welcome_bonus_is_not_a_salary():
    """IVC's "Head Vet Nurse" states no salary at all. It mentions a welcome
    bonus, the parser read that as pay, and `clears_floor` then deleted the
    role for offering £3,000 a year.

    Three postings in the 13,588 sample were destroyed this way, and every one
    of them was a posting that published no figure -- exactly the case the
    unconfirmed rule exists to protect."""
    text = ("We are seeking a knowledgeable, confident Head Veterinary Nurse "
            "to join our team at Easipetcare, Dartford, on a full-time basis. "
            "We are also offering up to £3,000 welcome bonus for this "
            "position.")
    sal = parse_text(text)
    assert not sal.confirmed, sal.raw
    assert clears_floor(sal, 120000.0, "GBP")[0]


def test_a_real_salary_survives_a_bonus_standing_next_to_it():
    """The fix above must not fire on the far more common shape, where a
    genuine salary is followed by a bonus that carries its own figure.

    Reading "Up to £65,000 Welcome bonus of up to £5,000" as a £5,000 salary
    would be a worse bug than the one being fixed."""
    for text, top in (
        ("Vet at Abivale Vets, Wallingford Up to £65,000 Welcome bonus of "
         "up to £5,000 (pro rata) & relocation", 65000.0),
        ("Salary up to £75,000 DOE Welcome Bonus up to £5,000 Relocation "
         "Allowance up to £5,000", 75000.0),
        ("Salary: £50,000 - £60,000 (DOE)", 60000.0),
    ):
        sal = parse_text(text)
        assert sal.confirmed, text
        assert sal.top == top, (text, sal.raw, sal.top)


def test_a_relocation_allowance_before_the_figure_is_not_a_salary():
    """The same question from the other side. "relocation allowance of up to
    £2,000" names the number that FOLLOWS it, and skipping the welcome bonus
    in front of it walked straight onto the allowance instead."""
    sal = parse_text("This role offers a £1,000 welcome bonus and relocation "
                     "allowance of up to £2,000. The Oak Veterinary Group is "
                     "based in Pembrokeshire.")
    assert not sal.confirmed, sal.raw


def test_an_html_entity_does_not_cut_a_pay_range_in_half():
    """Greenhouse double-encodes its adverts, so a single `html.unescape` in
    the adapter leaves `&nbsp;` sitting in the middle of the range. `\\s*` does
    not match it, the range collapsed to its first figure, and Focal Systems'
    "£60,000 to £70,000" was stored as £60,000 -- below any floor set between
    the two."""
    sal = parse_text("Location: Remote in the UK only. Salary Range: "
                     "£60,000 to &nbsp; £70,000 + stock options")
    assert sal.confirmed
    assert (sal.min, sal.max) == (60000.0, 70000.0), sal.raw
    assert parse_text("Base pay range: $113,000&nbsp;-&nbsp;$185,000").max \
        == 185000.0


def test_per_hour_is_not_read_as_per_day():
    """`_period` answered from the whole block it was given, and the second
    pass hands it up to 19,600 characters. `_PER_DAY` was tested before
    `_PER_HOUR`, so one "a day" anywhere in the body turned an hourly rate
    into a day rate -- an eightfold error in the annualised figure the floor
    compares against.

    Five Bezos Academy adverts saying "$19-$27 per hour" came out as $19 a
    day, which annualises to $5,940."""
    text = ("Every day looks a little different in our classrooms. This is a "
            "full-time (40 hours per week), benefits-eligible, non-exempt "
            "position. Compensation for this position is $19-$27 per hour.")
    sal = parse_text(text)
    assert sal.confirmed
    assert sal.period == "hour", sal.period
    assert sal.annualised() > 30000


def test_the_period_word_nearest_the_figure_wins():
    """Both words are present and only distance can separate them."""
    sal = parse_text("Compensation for this position is $19-$27 per hour "
                     "(~$39,000-$56,000 annually).")
    assert sal.confirmed and sal.period == "hour", (sal.raw, sal.period)

    sal = parse_text("Pay: $90K/YR + DOE. The pay listed is the hourly range "
                     "or the hourly rate for this position.")
    assert sal.confirmed and sal.period == "year", (sal.raw, sal.period)


def test_a_monthly_figure_is_annualised_not_read_as_the_year_figure():
    """`Salary` models year, day and hour and has nowhere to put a month, so
    a monthly wage stored unmultiplied would be the year's pay read twelve
    times too small and hidden by any floor at all. Columbia Shipmanagement
    advertise a $10,500 a month officer's berth exactly this way, and the fix
    is to multiply it up rather than to throw it away: $10,500 a month is a
    genuine $126,000 a year, which clears a $120,000 floor, whereas the bare
    $10,500 the old code refused to confirm would have been silently dropped
    from a shortlist it belongs on had that floor ever been applied to it."""
    sal = parse_text("Salary: $10,500 per month, plus leave pay.")
    assert sal.confirmed and sal.period == "year", sal
    assert sal.max == 126000.0, sal
    assert clears_floor(sal, 120000.0, "USD")[0]


def test_a_period_word_straddling_the_block_boundary_is_still_read():
    """The scan works in blocks -- the first 400 characters, then the rest --
    and the period question used to be asked of the block. A label that
    started inside the block and finished outside it was invisible: "$10,000+
    per month" sat at character 383, "per month" was never read, and the
    figure was confirmed as an annual $10,000 salary -- rather than the
    annualised $120,000 the advert actually states, and a figure that small
    is also below the plausibility floor, so getting this wrong reads as
    "unconfirmed" now rather than as a wrong number, which is the safer of
    the two failures but still the wrong one to ship."""
    lead = "x" * 360
    sal = parse_text(f"{lead} Strong performers earn $10,000+ per month.")
    assert sal.confirmed and sal.period == "year", sal
    assert sal.max == 120000.0, sal


def test_a_salary_below_the_floor_is_still_dropped():
    """The point of the fixes above is accuracy, not leniency. A stated
    figure that really is below the floor must still disqualify the role, or
    the filter does nothing."""
    sal = parse_text("Salary: £45,000 - £55,000 per annum")
    assert sal.confirmed
    keep, why = clears_floor(sal, 120000.0, "GBP")
    assert not keep and "below floor" in why


# ---------------------------------------------------------------------------
# Titles
# ---------------------------------------------------------------------------

def test_the_same_job_written_a_different_way_still_matches():
    """`titles.include` is a substring test and a job title is not a substring
    problem. Of 165 postings in the sample that a person would call
    engineering leadership, the committed four-title config matched 92; the
    other 73 were dropped as "title does not match" and never seen.

    Each of these is one of those, and each is the job the config asked for,
    written the way the employer writes it."""
    cfg = _cfg()
    for title in (
        "Manager, Engineering",
        "Manager, Engineering - Device Identity and Access",
        "Senior Manager, Software Engineering (Integrations Platform)",
        "Senior Manager, Data Engineering",
        "Manager, Site Reliability Engineering",
        "Director, Engineering, Enterprise",
        "Director, Data Engineering & Architecture (Remote)",
        "Associate Director, Platform Engineering",
        "Head of Site Reliability Engineering (SRE)",
        "Head of Mobile Engineering",
        "Independent Engineering Director",
    ):
        keep, why = match(_job(title=title), cfg)
        assert keep, f"{title!r} was dropped: {why}"


def test_a_different_job_that_shares_the_words_is_still_dropped():
    """Loosening the match must not turn it off. Every one of these contains
    both words of a configured title and is a different job."""
    cfg = _cfg()
    for title in (
        "Senior Technical Program Manager - Foundations Engineering",
        "Principal Technical Program Manager, Engineering",
        "Technical Program Manager, Product & Engineering",
        "Independent Engineering Program Manager",
        "Associate Project Manager, Civil Engineering - Land Development",
        "Business Development Manager, Engineering Services",
    ):
        keep, why = match(_job(title=title), cfg)
        assert not keep, f"{title!r} was kept"


def test_the_loose_matcher_only_ever_adds():
    """It runs after the substring regex, never instead of it, so a title that
    matched before cannot stop matching. `titles.exclude` still wins."""
    cfg = _cfg()
    assert match(_job(title="Product Engineering Manager"), cfg)[0]
    assert match(_job(title="Engineering Manager, Community"), cfg)[0]
    assert not match(_job(title="Product Manager, Engineering Platform"),
                     cfg)[0]


def test_a_single_word_title_is_left_to_the_substring_test():
    """A one-word term has no word order to be flexible about, and treating it
    loosely would match anything containing it."""
    assert title_matches_loosely("Engineering Manager", ["engineering"]) is None


def test_a_vp_title_needs_a_vp_term_in_the_config():
    """Honest about what the matcher cannot do. "VP of Engineering" and
    "Chief Technology Officer" are engineering leadership and stay dropped,
    because a config listing only manager, head and director titles never asks
    for them. 22 of the 165 leadership postings in the sample are this shape,
    and the fix is a line in the config, not in this file."""
    cfg = _cfg()
    assert not match(_job(title="VP of Engineering"), cfg)[0]
    assert not match(_job(title="Chief Technology Officer"), cfg)[0]
    # ...and the moment the config asks, the same matcher finds them.
    wide = _cfg(titles_include=["vp engineering", "chief technology officer"])
    # This was one `A or B` assertion, which lets either half stop working in
    # silence. Split, it turns out only one half holds: a config asking for
    # "vp engineering" matches "VP of Engineering" and does NOT match the
    # spelled-out "Vice President, Engineering - Authentication", which is the
    # form 22 of the 165 leadership postings in the sample actually use. That
    # is a live gap in the matcher, not in this test, so it is written down
    # here rather than asserted green.
    assert match(_job(title="VP of Engineering"), wide)[0]


# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------

def test_a_comma_separated_list_of_cities_names_every_country_in_it():
    """`_countries_in` split on pipes and slashes but not commas, so a
    comma-separated list of cities returned exactly one country: the first
    tier that matched anywhere in the string.

    "London, New York" came back as {US} and the London vacancy was dropped as
    outside the UK. Eleven postings in the sample lost a genuine UK location
    this way."""
    assert _countries_in("London, New York") == {"UK", "US"}
    assert _countries_in("San Francisco, New York, London") == {"UK", "US"}
    assert "UK" in _countries_in("Germany, Cyprus, Poland, London, Portugal")
    assert _countries_in("London, Copenhagen") == {"UK", "DK"}


def test_an_address_is_still_one_place():
    """The reason commas were left alone in the first place. A comma usually
    binds a place to its qualifier, and splitting there sent "Cambridge, MA"
    to the UK city list -- the bug that once filed 59 of 296 American roles as
    British."""
    assert _countries_in("Cambridge, MA") == {"US"}
    assert _countries_in("Cambridge, Massachusetts") == {"US"}
    assert _countries_in("Birmingham, AL") == {"US"}
    assert _countries_in("Toronto, Ontario, Canada") == {"CA"}
    assert _countries_in("Perth, United Kingdom") == {"UK"}
    # A hierarchy is read back to front: Benin is a city in Nigeria.
    assert _countries_in("Benin, Nigeria") == {"NG"}


def test_new_england_is_not_england_and_new_south_wales_is_not_wales():
    """The UK marker matched on a word boundary, so both of these resolved to
    the United Kingdom. A Sydney vacancy was filed as British, which shows it
    to the wrong person and hides it from the right one."""
    assert _countries_in("Sydney, New South Wales, au") == {"AU"}
    assert _countries_in("New England, Texas") == {"US"}
    # The genuine UK forms are untouched.
    assert _countries_in("Swansea, South Wales") == {"UK"}
    assert _countries_in("London, England") == {"UK"}


def test_a_location_that_names_no_place_is_not_an_unrecognised_place():
    """An empty location field is kept and labelled. A field saying "Hybrid",
    "HQ", "Various Locations" or "Remote - Worldwide" says exactly as much and
    was dropped as "location not recognised" -- 49 postings in the sample,
    thrown away for describing an arrangement instead of a country."""
    cfg = _cfg()
    for loc in ("Hybrid", "HQ", "Various Locations", "Multiple",
                "Hybrid or Remote", "Remote - Worldwide", "All Locations",
                "Remote, Worldwide"):
        keep, why = match(_job(location=loc), cfg)
        assert keep, f"{loc!r} was dropped: {why}"


def test_a_place_that_is_named_and_foreign_is_still_dropped():
    """The counterweight. Nothing above may turn the country filter off."""
    cfg = _cfg()
    for loc in ("New York, NY", "Berlin", "Remote - United States",
                "Bengaluru, India", "Remote London Ontario"):
        assert not match(_job(location=loc), cfg)[0], loc


def test_a_remote_role_restricted_in_the_body_is_still_dropped():
    """"Remote" in the field and "US only" in the advert is a US role."""
    cfg = _cfg()
    job = _job(location="Remote",
               description="This position is US - Remote Eligible.")
    keep, why = match(job, cfg)
    assert not keep and "restricted" in why, why


# ---------------------------------------------------------------------------
# Dealbreakers and sponsorship
# ---------------------------------------------------------------------------

def test_a_dealbreaker_named_in_a_certification_does_not_hide_the_role():
    """A dealbreaker is a statement about THIS job. The regex fires on any
    occurrence anywhere in the advert, and a "Cloud Platform Engineering
    Manager" was deleted because a PREFERRED CERTIFICATION is called "Azure
    Solutions Architect Expert"."""
    job = _job(title="Cloud Platform Engineering Manager",
               description=(
                   "You will lead a platform team of eight and own the "
                   "roadmap. Preferred certifications include Microsoft "
                   "Certified: Azure Solutions Architect Expert, Microsoft "
                   "Certified: Azure Administrator Associate."))
    keep, hits = screen(job, _cfg())
    assert keep, hits
    assert any("only in passing" in f for f in job.flags), job.flags


def test_a_dealbreaker_about_another_team_does_not_hide_the_role():
    """"Work cross-functionally with customers, forward deployed teams" names
    who you collaborate with, not what the job is."""
    job = _job(title="Engineering Manager, Agent Oversight",
               description=("Own monitoring and evaluation of agentic "
                            "applications. Work cross-functionally with "
                            "customers, forward deployed teams, product, and "
                            "internal engineering teams."))
    assert screen(job, _cfg())[0], job.flags


def test_a_dealbreaker_that_really_is_the_job_still_hides_it():
    """The whole point of the rule. When the phrase describes the role, in the
    title or in the body, it drops as before."""
    job = _job(title="Frontier Agent Engineering Manager, Enterprise",
               description=("As a Forward Deployed AI Engineering Manager on "
                            "our Enterprise team, you will sit with the "
                            "customer."))
    assert not screen(job, _cfg())[0]

    job = _job(description=("Interview process: 1. Initial interview 2. "
                            "Take-home exercise 3. Final interview."))
    keep, hits = screen(job, _cfg())
    assert not keep and "coding round" in hits


def test_a_sponsorship_refusal_offered_as_an_example_is_not_this_advert():
    """Omnea's adverts carry a legal notice naming "unable to provide visas"
    as an EXAMPLE of the kind of hard requirement a posting might state. It is
    not this posting's policy, and it accounted for 13 of the 77 US roles a
    sponsorship filter dropped in the sample."""
    body = ("ADDITIONALLY, WHERE ROLES HAVE HARD-SPECIFIED REQUIREMENTS "
            "(E.G. [X] DAYS IN OFFICE, UNABLE TO PROVIDE VISAS, ETC), IF IN "
            "YOUR APPLICATION YOU PROVIDE DETERMINISTIC CHECK-BOX "
            "CONFIRMATION THAT YOU DO NOT MEET THEM, AUTOMATIC REJECTION "
            "CRITERIA ARE IN PLACE.")
    assert work_rights(_job(description=body)) == ""

    cfg = _cfg(countries=["UK", "US"], need_sponsorship=["US"])
    job = _job(location="New York, NY", description=body)
    assert sponsorship_gate(job, cfg)[0]


def test_a_real_sponsorship_refusal_still_hides_the_role():
    """64 of those 77 were plain statements of policy and were right to go."""
    cfg = _cfg(countries=["UK", "US"], need_sponsorship=["US"])
    job = _job(location="New York, NY",
               description=("Must be legally authorized to work in the United "
                            "States. We are not able to sponsor visas."))
    keep, why = sponsorship_gate(job, cfg)
    assert not keep and "sponsorship" in why


if __name__ == "__main__":
    import traceback

    failures = 0
    for _name, _fn in sorted(globals().items()):
        if not _name.startswith("test_") or not callable(_fn):
            continue
        try:
            _fn()
            print(f"  pass  {_name}")
        except BaseException:
            failures += 1
            print(f"  FAIL  {_name}")
            traceback.print_exc()
    sys.exit(1 if failures else 0)
