"""SuccessFactors' unified JSON search, the platform BT Group's jobs.bt.com
is on.

`fetch_rmk`'s HTML scan finds nothing here: the plain page carries no
`/job/` link at all, because SuccessFactors' newer "unified" front end
builds the whole results area in JavaScript from this JSON endpoint. Before
this adapter existed, BT Group was configured with `platform: rmk`, answered
HTTP 200 with a 172KB shell holding no rows a regex could find, and was
read as a board with no vacancies -- silently, next to every employer that
genuinely has none. `tests/test_rmk_unreadable.py` covers that failure and
its fix; every assertion here is about the JSON adapter itself, on a
trimmed real response captured from jobs.bt.com on 2026-09-26.

Every assertion is a field, not a count, for the same reason
`test_pcsx_adapter.py` gives: a parser that returns the right number of rows
with an empty column is this repo's signature failure.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import adapters
from jobradar.adapters import platforms
from jobradar.models import Source

FIXTURE = Path(__file__).parent / "fixtures" / "rmk_json_search.json"
SRC = Source(
    company="BT Group",
    url="https://jobs.bt.com/search/?q=&sortColumn=referencedate&sortDirection=desc",
    platform="rmk_json",
    country="gb",
)


def _payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _jobs():
    return list(platforms.parse_rmk_json(_payload(), SRC))


class RmkJsonFields(unittest.TestCase):
    def setUp(self):
        self.jobs = _jobs()

    def test_every_row_becomes_a_job(self):
        # The fixture carries one real repeated id (52967, seen twice live on
        # 2026-09-26) on purpose, so the row count and the job count are NOT
        # the same number: that gap is `test_a_repeated_id_is_kept_once`.
        payload = _payload()
        self.assertEqual(len(payload["jobSearchResult"]), 7)
        self.assertEqual(len(self.jobs), 6)

    def test_every_job_has_a_title(self):
        self.assertTrue(all(j.title.strip() for j in self.jobs))

    def test_every_job_has_a_location(self):
        missing = [j.title for j in self.jobs if not j.location.strip()]
        self.assertEqual(missing, [])

    def test_a_multi_location_role_keeps_every_office_not_a_count(self):
        """Software Developer (id 61472) is open in four BT offices.
        Collapsing that to a number is Workday's "2 Locations" fault, which
        sits exactly where a place would and reads as one."""
        role = next(j for j in self.jobs if j.title == "Software Developer")
        self.assertNotIn("4 Locations", role.location)
        for office in ("Cheltenham", "Ipswich", "London", "Manchester"):
            self.assertIn(office, role.location)
        # Joined with "; ", the same separator PCSX and Google Careers use,
        # never plain ", " -- that would read as one long place name.
        self.assertIn("; ", role.location)

    def test_a_location_has_no_trailing_comma_from_the_template(self):
        """BT's own field is "Euston TE, London, United Kingdom, " -- a
        trailing ", " left over from a template that assumes a fourth
        column. Stored as-is it reads as a typo on every single-office role."""
        for j in self.jobs:
            for part in j.location.split("; "):
                self.assertFalse(part.endswith(","), j.location)
                self.assertEqual(part, part.strip())

    def test_every_job_has_its_own_posting_url(self):
        urls = [j.url for j in self.jobs]
        self.assertTrue(all(u.startswith("https://jobs.bt.com/job/") for u in urls))
        self.assertEqual(len(set(urls)), len(urls))

    def test_the_url_carries_the_title_slug_not_just_the_id(self):
        """Verified live: `/job/<id>-<locale>` with no title slug 200s into
        BT's own generic 404 page rather than the advert, so a URL missing
        the slug is a dead link stored as a real one. `/job/<urlTitle>/<id>
        -<locale>` is the one SuccessFactors' own results widget builds and
        the one that resolves."""
        role = next(j for j in self.jobs if j.title == "Retail Advisor")
        self.assertEqual(
            role.url,
            "https://jobs.bt.com/job/Retail-Advisor/62644-en_GB")

    def test_a_title_with_a_comma_still_gets_a_url_encoded_slug(self):
        role = next(j for j in self.jobs
                    if j.title.startswith("Research Specialist,"))
        self.assertIn("Research-Specialist%2C-Sustainable-Network-Optimisation",
                      role.url)
        self.assertNotIn(",", role.url)
        self.assertNotIn(" ", role.url)

    def test_every_job_has_a_posted_date_in_iso(self):
        for j in self.jobs:
            self.assertIsNotNone(j.posted_at, j.title)
            self.assertRegex(j.posted_at, r"^\d{4}-\d{2}-\d{2}$")

    def test_the_platform_and_source_are_carried(self):
        self.assertTrue(all(j.platform == "rmk_json" for j in self.jobs))
        self.assertTrue(all(j.source_id == SRC.key for j in self.jobs))

    def test_the_rows_own_brand_wins_over_the_sources_company(self):
        """BT Group's board also carries EE's and its own generic
        "Default BT Group" postings under their own name, the same choice
        `parse_google_careers` makes for DeepMind, Waymo and YouTube: relabel
        every row "BT Group" and a reader looking for EE cannot find them."""
        by_title = {j.title: j.company for j in self.jobs}
        self.assertEqual(by_title["Retail Advisor"], "EE")
        self.assertEqual(by_title["Software Developer"], "BT Group")
        self.assertEqual(
            by_title["Register Your Interest Grads & Apprenticeships 2027"],
            "Default BT Group")

    def test_a_repeated_id_is_kept_once(self):
        """Verified live 2026-09-26: 47 of 212 rows fetched across BT's whole
        board were repeats of an id already returned, up to three times for
        five of them. `id` 52967 is repeated twice in this fixture on
        purpose."""
        titles = [j.title for j in self.jobs]
        self.assertEqual(titles.count("Infrastructure Monitoring Engineer"), 1)

    def test_the_description_is_empty_and_that_is_deliberate(self):
        # The search endpoint carries no advert text, the same as PCSX, and
        # nothing here is wired to fetch one: see the module docstring in
        # `jobradar/adapters/platforms.py` for why.
        self.assertTrue(all(j.description == "" for j in self.jobs))

    def test_salary_is_empty_rather_than_built_from_a_bare_currency_code(self):
        """The board states `currency` on almost every row and no amount
        anywhere. A `Salary(currency="GBP")` with no figure would pass the
        "has a salary" check on every dashboard row and screen against a
        floor it never stated."""
        for j in self.jobs:
            self.assertIsNone(j.salary.min)
            self.assertIsNone(j.salary.max)

    def test_the_listing_flags_itself_as_unscreened(self):
        for j in self.jobs:
            self.assertIn("not screened: search listing only, open the advert",
                          j.flags)

    def test_uids_are_distinct(self):
        self.assertEqual(len({j.uid for j in self.jobs}), len(self.jobs))

    def test_a_row_missing_an_id_or_title_is_dropped_not_stored_blank(self):
        broken = {"jobSearchResult": [
            {"response": {"unifiedStandardTitle": "No id",
                          "urlTitle": "No-id"}},
            {"response": {"id": "1", "urlTitle": "No-title"}},
            {"response": {"id": "2", "unifiedStandardTitle": "No slug"}},
        ], "totalJobs": 3}
        jobs = list(platforms.parse_rmk_json(broken, SRC))
        self.assertEqual(jobs, [])


class RmkJsonWiring(unittest.TestCase):
    def test_the_platform_matches_its_own_endpoint(self):
        self.assertEqual(
            adapters.detect(
                "https://jobs.bt.com/services/recruiting/v1/jobs").name,
            "rmk_json")

    def test_the_platform_does_not_shadow_the_html_rmk_boards(self):
        """`rmk`'s own regex matches `/search/?q=`, which every configured
        `rmk` source's URL contains. `rmk_json` must not also match those 93
        URLs, or `detect()` would misclassify a live board."""
        self.assertEqual(
            adapters.detect(
                "https://jobs.adidas-group.com/search/?q=&sortColumn=x"
            ).name,
            "rmk",
        )

    def test_a_page_is_ten_so_a_paging_bug_is_visible(self):
        from jobradar import fetch
        self.assertEqual(fetch.PAGE_SIZES["rmk_json"], 10)

    def test_the_registry_carries_the_new_platform(self):
        self.assertIsNotNone(adapters.by_name("rmk_json"))


if __name__ == "__main__":
    unittest.main()
