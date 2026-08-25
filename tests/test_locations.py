"""Where a posting is, which decides whether the user can take the job at all.

Kept separate from test_core.py so a country rule can be added without
touching the file every adapter's tests live in.
"""

import contextlib
import io
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar.screen import _countries_in, _country_of


def test_a_state_code_that_is_also_a_country_lets_the_city_decide():
    """Twenty US state codes are also ISO country codes, and the state check
    ran first, so it answered before the city was ever consulted.

    "Berlin, DE" came back as Delaware and "Toronto, CA" as California. Both
    were then filed as US roles: a country the user may need a visa for, and
    one they may have excluded outright."""
    assert _countries_in("Berlin, DE") == {"DE"}
    assert _countries_in("Toronto, CA") == {"CA"}
    assert _countries_in("Munich, DE") == {"DE"}
    assert _countries_in("Vancouver, CA") == {"CA"}


def test_the_same_codes_still_read_as_states_when_the_city_is_american():
    """The fix must not overshoot. San Francisco, CA is not Canada."""
    for loc in ("San Francisco, CA", "Sacramento, CA", "Atlanta, GA",
                "Chicago, IL", "Indianapolis, IN", "Los Angeles, CA"):
        assert _countries_in(loc) == {"US"}, loc


def test_an_ambiguous_code_needs_the_city_to_corroborate_it():
    """Letting the city win outright was the wrong fix, and would have
    reintroduced the bug it replaced.

    "Birmingham, AL" and "Reading, PA" both hit the UK city list, so deferring
    to the city would file two American roles as British, which is exactly
    what once mislabelled 59 of 296 US roles. The code only counts as a
    country when the city names that same country: Berlin corroborates DE,
    Birmingham does not corroborate Albania."""
    for loc in ("Birmingham, AL", "Reading, PA", "Bath, ME", "Manchester, NH"):
        assert _country_of(loc) == "US", loc

    # And with nothing on the city list either way, it stays a state.
    assert _country_of("Wilmington, DE") == "US"
    assert _country_of("Dover, DE") == "US"


def test_unambiguous_state_codes_are_unaffected():
    for loc in ("Austin, TX", "Seattle, WA", "Portland, OR", "Boston, MA"):
        assert _countries_in(loc) == {"US"}, loc


def test_countries_are_recognised_under_the_names_employers_write():
    """A German employer posting in German writes Deutschland, and the
    accented spelling of Sao Paulo is the usual one."""
    assert _countries_in("Deutschland") == {"DE"}
    assert _countries_in("São Paulo, BR") == {"BR"}
    assert _countries_in("Sao Paulo") == {"BR"}
    assert _countries_in("Rio de Janeiro") == {"BR"}


def test_a_location_naming_nothing_stays_unknown():
    """Unknown has to stay distinguishable from a country. "Remote" and
    "EMEA" name no country, and guessing one would put a role in front of
    someone who cannot take it."""
    for loc in ("Remote", "EMEA", "Worldwide", "Anywhere", ""):
        assert _countries_in(loc) == set(), loc


def test_a_platforms_own_remote_flag_beats_prose_in_the_advert():
    """Pinpoint, Breezy and Teamtailor all state the arrangement in a field,
    and the description scan ran first, so it answered first.

    An advert mentioning an on-site gym, on-site parking, or occasional
    on-site visits filed a role the ATS had marked remote as office based.
    Prose still decides when the platform set no flag, and an explicitly
    hybrid posting still wins over both."""
    from jobradar.models import Job
    from jobradar.screen import work_mode

    def job(**kw):
        return Job(company="Acme", title="Engineer", url="https://x/1",
                   platform="pinpoint", **kw)

    flagged = job(location="Remote", remote=True,
                  description="We have an on-site gym at the London office.")
    assert work_mode(flagged) == "remote"

    # No flag set, so the advert is all there is.
    assert work_mode(job(location="London",
                         description="This role is office based.")) == "office"

    # And an explicitly hybrid posting is hybrid whatever the flag says.
    assert work_mode(job(location="London", remote=True,
                         description="Hybrid, 3 days a week in the office.")) == "hybrid"


def test_an_aggregator_never_outranks_the_employers_own_board():
    """Dedupe picks a winner by directness first and description length
    second. Reed returns full advert text, so at the default score a Reed
    repost could take the row from the employer's own posting and hand the
    reader a reed.co.uk redirect instead of the real apply page."""
    from jobradar.screen import directness

    assert directness("reed") < directness("greenhouse")
    assert directness("linkedin") < directness("reed")
    assert directness("greenhouse") == directness("workday") == 2


# ------------------------------------------------- aggregators and dedupe
def _row(company, title, platform, desc="x" * 120, location="London"):
    from jobradar.models import Job
    return Job(company=company, title=title, url=f"https://{platform}/1",
               platform=platform, location=location, description=desc)


def test_the_employers_own_board_wins_over_an_aggregator_reposting_it():
    """Adding aggregators means the same role arrives twice, and the copy the
    reader wants is the one that links to the real apply page rather than a
    redirect."""
    from jobradar.screen import dedupe

    out = dedupe([_row("Monzo", "Engineering Manager", "reed"),
                  _row("Monzo", "Engineering Manager", "greenhouse")])
    assert len(out) == 1
    assert out[0].platform == "greenhouse"


def test_a_legal_form_or_descriptor_does_not_hide_the_duplicate():
    """An aggregator prints whatever the employer registered as. Grouping on
    the raw name left "Monzo Bank Ltd" and "Monzo" in separate groups, so both
    rows showed and directness never got to decide."""
    from jobradar.screen import dedupe

    for agg_name, direct_name, plat in (
            ("Monzo Bank Ltd", "Monzo", "greenhouse"),
            ("BT Group plc", "BT", "workday"),
            ("Wise Payments Limited", "Wise", "smartrecruiters")):
        out = dedupe([_row(agg_name, "Risk Manager", "reed"),
                      _row(direct_name, "Risk Manager", plat)])
        assert len(out) == 1, agg_name
        assert out[0].platform == plat
        assert any("also listed on" in f for f in out[0].flags)


def test_a_loose_name_match_never_collapses_two_real_employers():
    """The fuzzy half only ever folds an aggregator into a direct board. Sky
    and Skyscanner both run their own boards and both roles are real, so a
    prefix match must not be allowed to delete one of them."""
    from jobradar.screen import dedupe

    out = dedupe([_row("Sky", "Data Engineer", "greenhouse"),
                  _row("Skyscanner", "Data Engineer", "workday")])
    assert len(out) == 2


def test_two_different_roles_at_one_employer_stay_two_roles():
    """Titles still have to match exactly. Platform and Payments are two
    vacancies, and merging them would lose one."""
    from jobradar.screen import dedupe

    out = dedupe([_row("Monzo", "Engineering Manager, Platform", "greenhouse"),
                  _row("Monzo Bank Ltd", "Engineering Manager, Payments", "reed")])
    assert len(out) == 2


def test_an_agency_repost_is_left_alone_because_it_names_the_agency():
    """A role posted by Robert Walters carries Robert Walters as the employer,
    so no name rule can tie it to Monzo. Reed's postedByDirectEmployer filter
    is what handles that, at fetch time, not dedupe."""
    from jobradar.screen import dedupe

    out = dedupe([_row("Monzo", "Engineering Manager", "greenhouse"),
                  _row("Robert Walters", "Engineering Manager", "reed")])
    assert len(out) == 2


def test_a_postings_own_location_beats_the_boards_country_tag():
    """A board is tagged with where its vacancies usually are. That is a fair
    default and a bad override.

    Homebase's board is tagged UK because Homebase is a UK retailer, and the
    Ashby adapter sets no country, so `j.country or source.country` filed a
    genuine Toronto vacancy as UK. 23 roles were stored UK while their own
    location said US."""
    from jobradar.screen import _countries_in

    from jobradar.sources import NON_COUNTRY_TAGS, normalise_country_tag

    def resolve(location, tag):
        here = _countries_in(location or "")
        # As cli.py does it: the list is normalised onto one spelling as it is
        # loaded, and the tag is only a country if it is not one of the two
        # tags that mean "not a country".
        tag = normalise_country_tag(tag)
        if tag in NON_COUNTRY_TAGS:
            tag = ""
        if len(here) == 1:
            return here.pop()
        if here:
            return tag if tag in here else ""
        return tag

    assert resolve("Toronto", "UK") == "CA"
    assert resolve("Berlin", "UK") == "DE"
    # Only when the posting names nowhere does the board's tag get used.
    assert resolve("", "UK") == "UK"
    assert resolve("Remote", "UK") == "UK"
    # "multiple" is not a country and must never be stored as one.
    assert resolve("", "multiple") == ""
    # Several countries named: the tag is usable only if it is one of them.
    assert resolve("London / New York", "UK") == "UK"
    assert resolve("Berlin / Paris", "UK") == ""


# ------------------------------------------------------------ avature links
def test_avature_reads_pipelines_and_query_string_ids_not_just_slugs():
    """Reading only `/JobDetail/<slug>` reported whole boards as empty.

    A pipeline is Avature's evergreen requisition and it is a real vacancy:
    HSBC's board carries 96 `/PipelineDetail/` links and zero `/JobDetail/`
    ones. Separately, Macquarie and Ross Stores put the id in the query
    (`/JobDetail?jobId=23921`) with no slug at all."""
    from jobradar.adapters.platforms import _AV_LINK

    for html, want in (
            ('<a href="https://x/careers/JobDetail?jobId=23921">Automation Engineer</a>',
             "https://x/careers/JobDetail?jobId=23921"),
            ('<a href="https://x/external/PipelineDetail/GSC-Manager">GSC: Manager</a>',
             "https://x/external/PipelineDetail/GSC-Manager"),
            ('<a href="https://x/careers/JobDetail/Some-Role">Some Role</a>',
             "https://x/careers/JobDetail/Some-Role")):
        got = _AV_LINK.findall(html)
        assert got and got[0][0] == want, html


def test_an_avature_share_link_is_still_not_a_job():
    """Every card carries share links whose QUERY STRING holds the job's own
    URL, so admitting a question mark anywhere before the match reports three
    rows per job. The query form is allowed only as `?jobId=<digits>`."""
    from jobradar.adapters.platforms import _AV_LINK

    tweet = ('<a href="https://twitter.com/share?text=Some Role '
             'https://x/careers/JobDetail/Some-Role">Share</a>')
    assert _AV_LINK.findall(tweet) == []


def test_the_same_avature_job_linked_twice_is_one_row():
    """A card links the record on its title and again on a View Job button.
    Keeping whichever came first would be luck; the labelled one is real."""
    from jobradar.adapters.platforms import parse_avature
    from jobradar.models import Source

    page = ('<a href="https://x/external/PipelineDetail/GSC-Manager">'
            'GSC: Senior Control Manager (Cyber)</a>'
            '<a href="https://x/external/PipelineDetail/GSC-Manager">View Job</a>')
    jobs = list(parse_avature(page, Source(company="HSBC", platform="avature",
                                           url="https://x/external/SearchJobs/")))
    assert len(jobs) == 1
    assert jobs[0].title == "GSC: Senior Control Manager (Cyber)"


def test_a_personal_config_is_preferred_over_the_one_that_ships():
    """`discover <employer> --add` wrote a personal board into config.yaml,
    which is the file the repo distributes. config.local.yaml is the personal
    one and is gitignored, so anything added there stays where it belongs."""
    import os, tempfile
    from pathlib import Path
    from jobradar.cli import _cfg_path

    here = os.getcwd()
    d = tempfile.mkdtemp()
    try:
        os.chdir(d)
        Path("config.yaml").write_text("titles:\n", encoding="utf-8")
        assert _cfg_path(None).name == "config.yaml"      # no personal one yet
        Path("config.local.yaml").write_text("titles:\n", encoding="utf-8")
        assert _cfg_path(None).name == "config.local.yaml"
        # An explicit path always wins, in either direction.
        assert _cfg_path("config.yaml").name == "config.yaml"
    finally:
        os.chdir(here)


# --------------------------------------------------- country resolution, 2026
# A tagging run over the whole bundled list read 433,955 live postings and
# 94,841 of them (21.9%) carried a location `_countries_in` could not place.
# Every test below is a shape taken from that run's `unrecognised_samples`,
# weighted by how many postings actually carried it, not a case anyone
# invented.

def test_a_lowercase_country_code_at_the_end_names_the_country():
    """The single biggest fixable shape in the data: 15,915 unresolved
    postings, 21.4% of everything unresolved, end in a lowercase ISO code.

    "Aachen, NRW, de", "Chennai, in", "Sofia, bg" and "Budapest, hu" are how a
    whole family of boards writes a location, and none of them resolved, so
    every German, Indian and Bulgarian role on those boards was invisible to
    the country filter in both directions."""
    assert _countries_in("Sofia, bg") == {"BG"}
    assert _countries_in("Budapest, hu") == {"HU"}
    assert _countries_in("Riga, lv") == {"LV"}
    assert _countries_in("Vilnius, Vilnius County, lt") == {"LT"}
    assert _countries_in("Campinas, SP, br") == {"BR"}
    assert _countries_in("Makati City, NCR, ph") == {"PH"}
    # And it beats the city list, which is the point of putting it first:
    # the UK entry would otherwise claim Newcastle for Britain.
    assert _countries_in("Newcastle, au") == {"AU"}


def test_a_lowercase_code_that_is_also_a_state_still_needs_corroborating():
    """23 ISO codes are also US state codes, and the rule for the uppercase
    form applies unchanged to the lowercase one.

    A province or Land corroborates it, and so does a city: "Calgary, AB, ca"
    and "Chennai, in" are safe. Nothing in "Savannah, ga" names Gabon, so it
    stays unresolved rather than being filed on the far side of the world.
    And a US city written in lowercase must still read as the state."""
    assert _countries_in("Montréal, QC, ca") == {"CA"}
    assert _countries_in("Whitecourt, AB, ca") == {"CA"}
    assert _countries_in("Aachen, NRW, de") == {"DE"}
    assert _countries_in("Chennai, in") == {"IN"}
    assert _countries_in("Barasat, WB, in") == {"IN"}

    assert _countries_in("Savannah, ga") == set()
    assert _countries_in("Los Angeles, ca") == {"US"}
    assert _countries_in("San Francisco, ca") == {"US"}


def test_the_corroborating_code_is_read_case_sensitively():
    """The province and Land codes are matched against the string as written,
    the same rule `_US_STATE` already follows.

    Reading them case-insensitively would let the ordinary English words "on",
    "by", "he", "st", "as" and "up" corroborate Canada, Germany and India, so
    "hands on, ca" would come back as a Canadian role."""
    assert _countries_in("hands on, ca") == set()
    assert _countries_in("Kingston, ON") == {"CA"}


def test_a_uk_town_or_county_resolves_without_the_word_britain():
    """The person running this filters on countries: [UK], so a UK town that
    does not resolve is a role they never see.

    BambooHR sends no country at all for office and hybrid roles and Reed
    sends free text, so "Farnborough", "Stoke-on-Trent" and "Cambridgeshire"
    all arrived unknown and were dropped as unplaceable. The county names are
    the same story: Reed sends "North Yorkshire" and "Dorset" as whole
    locations."""
    for loc in ("Farnborough", "Stoke-on-Trent", "Cambridgeshire", "GRIMSBY",
                "lincolnshire", "North Yorkshire", "Dorset", "Horsham",
                "Seaham", "Hackney", "Basingstoke", "Wolverhampton"):
        assert _countries_in(loc) == {"UK"}, loc


def test_a_shire_is_british_but_new_hampshire_is_not():
    """No other country names its counties -shire, which makes the suffix a
    reliable signal and the one American state ending in it the only trap.

    "New Hampshire" and "New Hampshire - Remote" are US locations and reading
    them as British would put a role in front of someone who cannot take it."""
    assert _countries_in("Oxfordshire") == {"UK"}
    assert _countries_in("Hampshire") == {"UK"}
    assert _countries_in("New Hampshire") == {"US"}
    assert _countries_in("New Hampshire - Remote") == {"US"}
    # A comma and a state code still answer before the suffix is consulted.
    assert _countries_in("Berkshire, MA") == {"US"}


def test_utf8_that_was_decoded_as_latin1_is_put_back_together():
    """2,430 unresolved postings (3.3%) were mojibake, and "MÃ¼nchen",
    "KÃ¶ln" and "DÃ¼sseldorf" alone accounted for over 1,500 of them.

    The accented spellings were already on the city list. Only the bytes were
    wrong, so repairing them is worth more than any city anyone could add."""
    assert _countries_in("MÃ¼nchen") == {"DE"}
    assert _countries_in("KÃ¶ln") == {"DE"}
    assert _countries_in("ZÃ¼rich") == {"CH"}
    # A string that was never mangled must come through untouched.
    assert _countries_in("München") == {"DE"}


def test_accents_are_folded_before_matching_but_not_instead_of_it():
    """Matching is tried against the folded and the unfolded text both.

    Folding alone would break "München" and "Kraków", which are on the city
    list in their accented form; not folding at all leaves "Montréal",
    "Košice" and "București" unresolved."""
    assert _countries_in("Montréal") == {"CA"}
    assert _countries_in("Košice, Slovakia") == {"SK"}
    assert _countries_in("București, București, ro") == {"RO"}
    assert _countries_in("Kraków") == {"PL"}
    assert _countries_in("São Paulo") == {"BR"}


def test_a_country_nobody_had_typed_in_yet_is_still_a_country():
    """The marker table held 45 countries, so Qatar, Saudi Arabia, Taiwan,
    Colombia, Ukraine and forty others resolved to nothing at all.

    An ISO table is the right shape for this: a list of every country's name
    is small, stable and checkable, where another forty alternations is not."""
    for loc, want in (("Qatar", "QA"), ("Saudi Arabia", "SA"),
                      ("Taiwan", "TW"), ("Colombia", "CO"), ("Ukraine", "UA"),
                      ("Sri Lanka", "LK"), ("Kuwait", "KW"), ("Egypt", "EG"),
                      ("Serbia", "RS"), ("Kazakhstan", "KZ"),
                      ("Riyadh, Saudi Arabia", "SA"),
                      ("Belgrade, Serbia", "RS"),
                      ("Cox's Bazar, Bangladesh", "BD")):
        assert _countries_in(loc) == {want}, loc


def test_a_country_written_in_another_language_is_the_same_country():
    """A German employer writes Deutschland or Deutschlandweit, a Swiss one
    writes Schweiz, a Dutch one writes Nederland. 2,110 postings said
    Nederland and none of them resolved."""
    assert _countries_in("Deutschlandweit") == {"DE"}
    assert _countries_in("Naaldwijk, Nederland") == {"NL"}
    assert _countries_in("Schweiz") == {"CH"}
    assert _countries_in("Türkiye") == {"TR"}
    assert _countries_in("Österreich") == {"AT"}


def test_nederland_texas_is_still_in_texas():
    """Nederland TX and Nederland CO are real American towns, so the Dutch
    name is guarded. The guard was written "TX" first, against a string this
    file has already lowercased, which made it dead on arrival."""
    assert _countries_in("Nederland, TX") == {"US"}
    assert _countries_in("Nederland, CO") == {"US"}
    assert _countries_in("Nederland") == {"NL"}


def test_a_country_name_is_read_from_the_end_of_the_hierarchy():
    """A location hierarchy puts the country last, so the segments are read
    back to front.

    "Benin, Nigeria" is the Nigerian city of Benin, and taking the first
    segment that happens to be a country name would file it in Benin, a
    different country 400 miles away."""
    assert _countries_in("Benin, Nigeria") == {"NG"}
    assert _countries_in("Casablanca, Morocco") == {"MA"}


def test_a_state_named_on_its_own_is_still_the_united_states():
    """"California", "Texas" and "Maryland - Remote" are whole locations on
    plenty of boards, and the spelled-out rule wanted a leading comma, so a
    state on its own resolved to nothing."""
    for loc in ("California", "Texas", "Massachusetts", "Ohio",
                "Maryland - Remote", "Virginia-remote", "New Jersey"):
        assert _countries_in(loc) == {"US"}, loc


def test_a_state_code_with_only_a_space_in_front_still_counts():
    """"Dallas TX", "Tampa FL" and "Olathe KS" name a US state and none of
    them resolved, because the rule wanted a comma.

    Only the 29 codes that are not also ISO country codes are trusted this
    loosely. The other 23 keep needing a comma and corroboration, so a
    hypothetical "Berlin DE" cannot become an American role."""
    for loc in ("Dallas TX", "Tampa FL", "Olathe KS", "St. Louis MO"):
        assert _countries_in(loc) == {"US"}, loc


def test_a_canadian_province_names_canada_the_way_a_state_names_the_us():
    """Nothing read them, so "Winnipeg, Manitoba", "Brampton, ON" and
    "Calgary, AB" all arrived unknown.

    Newfoundland, Prince Edward Island and Saskatchewan are left out: their
    codes are also the Netherlands, Peru and Slovakia, so a bare ", NL" is a
    coin toss and stays unresolved rather than being guessed."""
    assert _countries_in("Winnipeg, Manitoba") == {"CA"}
    assert _countries_in("Brampton, ON") == {"CA"}
    assert _countries_in("Calgary, AB") == {"CA"}
    assert _countries_in("Etobicoke, Ontario") == {"CA"}
    assert _countries_in("Somewhere, NL") == set()


def test_taleos_hyphen_hierarchy_is_read_biggest_first():
    """Taleo flattens a JSON array into a hyphen-joined string. `parse_taleo`
    unpicks it in the adapter, but boards mirroring the format send the raw
    shape straight here.

    Only a leading two-letter code counts, which is why "Stoke-on-Trent" and
    "Aix-en-Provence" are left alone by it."""
    assert _countries_in("IL-Northbrook") == {"US"}
    assert _countries_in("TX-Plano Legacy") == {"US"}
    assert _countries_in("PH-National Capital-Quezon City, Metro Manila") == {"PH"}
    assert _countries_in("Nebraska-Omaha") == {"US"}
    assert _countries_in("Stoke-on-Trent") == {"UK"}
    assert _countries_in("Aix-en-Provence, Provence-Alpes-Côte d'Azur, fr") == {"FR"}


def test_georgia_the_country_is_told_apart_from_georgia_the_state():
    """A precision fix, not a recall one: "Tbilisi, Georgia" resolved to US,
    because the spelled-out state rule matched the last word of it.

    The country is recognised from its cities and never from the bare word,
    and the bare word is not claimed for the state either, because "Georgia"
    on its own is genuinely both."""
    assert _countries_in("Tbilisi, Georgia") == {"GE"}
    assert _countries_in("Batumi") == {"GE"}
    assert _countries_in("Atlanta, Georgia") == {"US"}
    assert _countries_in("Georgia") == set()


def test_the_united_states_can_be_spelled_with_two_full_stops():
    """The marker required the trailing "a", so "U.S. Remote" and "Remote
    U.S." matched nothing and 170 postings written that way arrived with no
    country, on a filter where the US is a relocation target."""
    assert _countries_in("U.S. Remote") == {"US"}
    assert _countries_in("Remote U.S.") == {"US"}
    assert _countries_in("U.S. (Remote)") == {"US"}


def test_a_shape_that_is_genuinely_ambiguous_is_left_alone():
    """A role filed under the wrong country is worse than one filed under
    none, because the reader acts on it.

    Durham is a US city of 280,000 and a UK city of 48,000; "Remote - CA" is
    California or Canada with nothing to separate them; and a name shared with
    a large American place (Norfolk, Lincoln, Portsmouth, Worcester, Dover,
    Windsor, Peterborough, Salisbury, Winchester, Richmond, Lancaster,
    Carlisle, Greenwich, Camden) is deliberately absent from the UK list."""
    for loc in ("Durham", "Remote - CA", "Norfolk", "Lincoln", "Portsmouth",
                "Worcester", "Windsor", "Peterborough", "Richmond"):
        assert _countries_in(loc) == set(), loc


def test_a_location_that_names_no_place_still_names_no_country():
    """17.4% of the unresolved corpus is unresolved correctly, and it has to
    stay that way. "2 Locations" is how Ashby summarises a multi-site
    vacancy, "In-Office" is a work mode, "HQ" is a building, and none of them
    is a country. Resolving any of them would put a role in front of someone
    who cannot take it."""
    for loc in ("2 Locations", "3 Locations", "Multiple Locations", "Hybrid",
                "In-Office", "Hybrid; In-Office", "Distributed", "Virtual",
                "HQ", "Headquarters", "Main Campus", "LATAM", "APAC",
                "South East Asia", "World Wide - Remote", "Alle Standorte"):
        assert _countries_in(loc) == set(), loc


def test_the_old_state_and_city_answers_are_all_unchanged():
    """Every tier added here runs alongside rules that were each won against
    a real bug, and the one that matters most is the city list that once
    marked 59 of 296 American roles as British. None of the new tiers may
    reach a string those rules already answered."""
    for loc, want in (("San Francisco, CA", "US"), ("Toronto, CA", "CA"),
                      ("Berlin, DE", "DE"), ("Birmingham, AL", "US"),
                      ("Reading, PA", "US"), ("Bath, ME", "US"),
                      ("Manchester, NH", "US"), ("Wilmington, DE", "US"),
                      ("Chicago, IL", "US"), ("Indianapolis, IN", "US"),
                      ("Lebanon, PA", "US"), ("London", "UK"),
                      ("Dublin, OH", "US"), ("Paris, TX", "US"),
                      ("London, Ontario", "CA")):
        assert _country_of(loc) == want, loc


def test_new_mexico_is_a_us_state_and_not_the_country_of_mexico():
    """The country-name tier runs before the US state tier, so an unguarded
    `\\bmexico\\b` answered "New Mexico" before either state rule was reached.

    Every New Mexico posting that did not also spell out "United States"
    resolved to MX. A user filtering on US would never have been shown a role
    in Albuquerque, Santa Fe or Los Alamos, and the role would not have been
    reported as missing: it was filed under a country, just the wrong one.
    Same guard shape as Nederland, Texas and Paris, Texas."""
    for loc in ("Albuquerque, New Mexico", "Santa Fe, New Mexico",
                "Los Alamos, New Mexico", "New Mexico", "New Mexico - Remote",
                "Rio Rancho, New Mexico, United States"):
        assert _country_of(loc) == "US", loc


def test_mexico_the_country_still_resolves_to_mexico():
    """The guard must not overshoot: it only refuses the word when "new"
    comes immediately before it."""
    for loc in ("Mexico", "Mexico City", "Guadalajara, Mexico",
                "Remote - Mexico", "Monterrey, MX"):
        assert _country_of(loc) == "MX", loc


# --------------------------------------------------- the new-user path
#
# Everything below came out of one run of the tool as a stranger would meet
# it: clone, install, setup, scan, serve. Each is something that either wrote
# where it should not have, or failed without saying so.

def test_setup_refuses_a_non_terminal_instead_of_asking_forever():
    """`job-radar setup < /dev/null` produced 474MB of output in 25 seconds.

    `_ask` returned the default on EOFError, and the two questions that loop
    until answered (the CV, and the job titles) treat an empty answer as no
    answer. Once stdin is at EOF every later input() raises at once, so the
    loop spun at full speed printing its please-answer text.
    """
    from jobradar import setup_wizard

    out = io.StringIO()
    # The guard this exercises is `sys.stdin.isatty()`, and that is exactly
    # the thing that is not stable across test runners: a real terminal, a
    # CI runner and a sandboxed shell each answer it differently, so pinning
    # it here is what makes the test assert the same thing everywhere rather
    # than passing or failing depending on who calls it.
    with mock.patch.object(sys.stdin, "isatty", return_value=False):
        with contextlib.redirect_stdout(out):
            rc = setup_wizard.run(Path("config.yaml"))
    assert rc == 1
    assert len(out.getvalue()) < 2000, "should be a sentence, not a torrent"
    assert "needs a terminal" in out.getvalue()
    assert "--defaults" in out.getvalue(), "must name the scriptable way"


def test_ask_raises_rather_than_accepting_every_default_at_eof():
    """The questions that do not loop were no better than the ones that did.

    They silently took the default for every remaining answer and wrote a
    config the user never saw a single line of.
    """
    from jobradar.setup_wizard import NoInput, _ask

    def eof(_):
        raise EOFError
    with mock.patch("builtins.input", eof):
        try:
            _ask("   Path to your CV", "some-default")
        except NoInput:
            return
    raise AssertionError("EOF was swallowed and the default returned")


def test_setup_never_writes_the_file_the_repo_ships():
    """`setup` on a fresh clone reported "Wrote config.yaml", a tracked file.

    `git status` then reported `M config.yaml`, 22 insertions and 43
    deletions, every later `git pull` conflicted, and on a public fork it was
    the user's own CV path that got committed. config.yaml is no longer
    tracked upstream, so writing it creates a file rather than editing one.
    """
    import subprocess
    r = subprocess.run(["git", "ls-files", "--error-unmatch", "config.yaml"],
                       cwd=Path(__file__).resolve().parent.parent,
                       capture_output=True, text=True)
    assert r.returncode != 0, "config.yaml is tracked again: setup will dirty a clone"


def test_config_paths_are_absolute():
    """`job-radar setup` run from ~ wrote ~/config.yaml and said "config.yaml".

    Cwd-relative is defensible; not saying which directory is not.
    """
    from jobradar.cli import _cfg_path, _cfg_write_path
    assert _cfg_path(None).is_absolute()
    assert _cfg_write_path("some/other.yaml").is_absolute()


def test_a_broken_config_is_not_reported_as_zero_roles():
    """`list` goes straight to the database and never loads the config.

    With `sectors: [manufacturing]`, a tag that does not exist, it printed
    `0 role(s)`. The config WAS loaded first, by the daily-sync nudge, whose
    blanket `except Exception: pass` threw the explanation away.
    """
    from jobradar.cli import main
    d = Path(tempfile.mkdtemp())
    cfg = d / "config.yaml"
    cfg.write_text("titles:\n  include: [engineer]\nsectors: [manufacturing]\n",
                   encoding="utf-8")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = main(["-c", str(cfg), "list", "--db", str(d / "x.db")])
    assert rc == 1
    assert "manufacturing" in out.getvalue()
    assert "0 role(s)" not in out.getvalue()


def test_dry_run_leaves_the_dashboard_alone():
    """`--dry-run` printed "nothing was recorded" and then wrote out/.

    True of the database, false of the filesystem. `scan --limit 200
    --dry-run`, the quick look the wizard recommends, replaced a full
    dashboard with a 200-source sample of one.
    """
    from jobradar.cli import main
    d = Path(tempfile.mkdtemp())
    cfg = d / "config.yaml"
    cfg.write_text(
        "titles:\n  include: [engineer]\n"
        "sources:\n  use_bundled: false\n"
        "  extra:\n    - {company: Nowhere, platform: greenhouse, url: 'https://example.invalid/x'}\n"
        f"cv:\n  path: {cfg}\n", encoding="utf-8")
    out = io.StringIO()
    with contextlib.redirect_stdout(out), \
            mock.patch("jobradar.cli.fetch_all", return_value=[]):
        rc = main(["-c", str(cfg), "scan", "--dry-run", "--no-enrich",
                   "--db", ":memory:", "--out", str(d / "out")])
    assert rc == 0, out.getvalue()
    assert not (d / "out").exists(), "a dry run wrote the dashboard"
    assert "left alone" in out.getvalue(), "and it should say so"


def test_the_board_count_in_the_header_cannot_go_stale():
    """`meta.boards` said 17,834 for a list of 17,807.

    Nothing maintained it. The weekly `validate --prune` rewrites the file and
    had no reason to think about a number in the header, so it drifted a
    little further every Sunday. It is now counted from what is being written.
    """
    import json
    from jobradar.models import Source
    from jobradar.sources import BUNDLED, save

    d = Path(tempfile.mkdtemp()) / "s.json"
    save([Source(company="A", platform="greenhouse", url="https://a.invalid"),
          Source(company="B", platform="greenhouse", url="https://b.invalid"),
          Source(company="Keyword", platform="linkedin",
                 url="https://x.invalid/{keyword}", keyword_template=True)],
         d, meta={"boards": 999, "note": "kept"})
    body = json.loads(d.read_text(encoding="utf-8"))
    assert body["meta"]["boards"] == 2, "keyword templates are not boards"
    assert body["meta"]["note"] == "kept", "the rest of the header survives"

    shipped = json.loads(Path(BUNDLED).read_text(encoding="utf-8"))
    real = sum(1 for x in shipped["sources"] if not x.get("keyword_template"))
    assert shipped["meta"]["boards"] == real, (
        f"header says {shipped['meta']['boards']}, list holds {real}")


def test_the_data_the_tool_reads_at_runtime_is_declared_for_the_wheel():
    """`skills/` shipped in a clone and in `pip install -e .`, not in a wheel.

    Both of those are directories outside the package, reachable only because
    setuptools was told about them. `sources/` was declared and survives;
    `skills/` was not, so a wheel install found no bundled skills and every CV
    was drafted without them. Verified by building a wheel: before, zero
    skill files in it; after, eight.

    This checks the declaration rather than building a wheel, because the
    build needs network. If a third runtime directory is ever added, add it
    here too.
    """
    root = Path(__file__).resolve().parent.parent
    decl = (root / "pyproject.toml").read_text(encoding="utf-8")
    for d in ("sources", "skills"):
        assert (root / d).is_dir(), f"{d}/ has moved; this test is now wrong"
        assert f"../{d}/" in decl, (
            f"{d}/ is read at runtime but not in package-data, so it will be "
            f"missing from a built wheel")


def test_discover_add_writes_through_the_same_guard_as_setup():
    """`--add` edits a config, so it is subject to setup's rule: never write
    the file the repo distributes. When the read and write paths were split,
    this caller was left on the read one, which is precisely the half of the
    bug that fb6cc68 already failed to fix once."""
    src = (Path(__file__).resolve().parent.parent / "jobradar" / "cli.py"
           ).read_text(encoding="utf-8")
    add = src[src.index("if args.add and good:"):][:600]
    assert "_cfg_write_path(args.config)" in add
    assert "_cfg_path(args.config)" not in add


def test_a_named_state_beats_a_city_that_exists_in_two_countries():
    """"Newcastle, New South Wales" was a British job.

    Stopping "New South Wales" from matching the Wales marker was necessary
    and not sufficient: Newcastle is a UK city hint, nothing named Australia,
    and the city won by default. A city name is the weaker signal of the two
    because plenty of them exist twice, while a state or province belongs to
    exactly one country. Perth is the pair that proves it has to cut both
    ways.
    """
    assert _countries_in("Newcastle, New South Wales") == {"AU"}
    assert _countries_in("Brisbane, Queensland") == {"AU"}
    assert _countries_in("Perth, Western Australia") == {"AU"}
    assert _countries_in("Perth, Scotland") == {"UK"}
    assert _countries_in("Victoria, British Columbia") == {"CA"}
    # Not broken on the way past.
    assert _countries_in("Cardiff, South Wales") == {"UK"}
    assert _countries_in("London, England") == {"UK"}
    assert _countries_in("London, New York") == {"UK", "US"}
