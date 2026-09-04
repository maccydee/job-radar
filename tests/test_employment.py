"""Permanent, contract, or the advert did not say.

Every string in this file came out of `data/job-radar.db`, and the negative
cases are not hypothetical: a first version of the classifier read the word
"contract" anywhere in a posting and called 773 of 5,474 roles contract work.
Reading the hits showed the true figure was 36. All 737 of the difference were
sentences like the ones below, and none of them looked wrong on the dashboard,
because a role wearing the wrong label renders exactly like a role wearing the
right one.

The three-value split is the other thing under test. `unstated` is the answer
for 97.8% of the board and it is not a synonym for `permanent`: a reader
hunting contract work who is shown only the rows proven permanent has had the
whole unstated middle of the market hidden, and a hidden role is
indistinguishable from a role that was never posted.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import employment
from jobradar.models import Job
from jobradar.screen import enrich

C, P, U = employment.CONTRACT, employment.PERMANENT, employment.UNSTATED


def v(title, desc=""):
    return employment.classify(title, desc)[0]


class TitlesThatSayContract(unittest.TestCase):
    """Real titles from the board, all of them contract or interim."""

    CASES = [
        "Product Designer - 12 Month FTC",
        "Staff Product Designer (12-Month FTC)",
        "Engineering Team Lead - 12 month FTC",
        "VP Excellence & Engineering (12 month FTC)",
        "Engineering Support Manager - FTC",
        "Director, Quality Engineering (12 month contract)",
        "Director of Engineering (Contract, LATAM)",
        "Retail Technology Director - contract - outside IR35",
        "Interim Head of Engineering",
        "Interim Senior Engineering Manager",
        "Fractional CTO",
        "Fractional Chief Technology Officer",
        "Site AI Transformation Lead (Secondment 12-18 Months)",
        "Engineering Manager - Maternity Cover",
        "Freelance Engineering Manager",
    ]

    def test_each_is_contract(self):
        for t in self.CASES:
            self.assertEqual(v(t), C, t)

    def test_each_carries_its_evidence(self):
        # A label with nothing behind it cannot be checked by the person
        # reading the board, and this classifier is a pile of regexes.
        for t in self.CASES:
            _, ev = employment.classify(t)
            self.assertTrue(ev.strip(), t)
            self.assertIn(ev.lower(), t.lower(), t)


class TitlesThatOnlyLookLikeContract(unittest.TestCase):
    """The word is there and the job is permanent."""

    def test_a_plural_is_the_subject_not_the_terms(self):
        # An employer offering contract work writes "Contract". An employer
        # hiring somebody to work on contracts writes "Contracts".
        self.assertEqual(v("Engineering Manager, Contracts Platform"), U)
        self.assertEqual(v("Head of Engineering - Contracts and Billing"), U)

    def test_contract_testing_is_a_test_suite(self):
        self.assertEqual(v("Engineering Manager, Contract Testing"), U)
        self.assertEqual(v("Manager, API Contract Tooling"), U)

    def test_smart_contracts_are_a_blockchain(self):
        self.assertEqual(v("Engineering Manager, Smart Contract Platform"), U)

    def test_commercial_contract_roles_are_not_contract_roles(self):
        for t in ("Contract Manager, Engineering",
                  "Engineering Manager - Vendor Contract Delivery",
                  "Head of Engineering, Government Contract Programmes"):
            self.assertEqual(v(t), U, t)


class DescriptionsThatSayContract(unittest.TestCase):
    """Phrases with no innocent reading left, checked against the whole corpus."""

    def test_ir35(self):
        self.assertEqual(v("Area Engineering Manager",
                           "Warrington Contract (Inside IR35) or Permanent"), C)

    def test_a_day_rate_with_money_next_to_it(self):
        self.assertEqual(
            v("Engineering Manager", "Rate: Up to £750/day (Inside IR35)"), C)
        self.assertEqual(v("Engineering Manager", "£550/day, SC required"), C)

    def test_a_fixed_term_contract(self):
        self.assertEqual(
            v("AI Engineering Lead", "Contract Type: Fixed Term Contract"), C)
        self.assertEqual(
            v("Director, Analytics Engineering",
              "This role is offered on a fixed-term basis."), C)

    def test_a_duration_attached_to_the_engagement(self):
        self.assertEqual(
            v("Head of Engineering, SAP",
              "This is an initial 6 month contract that will likely extend."), C)

    def test_the_advert_calling_the_role_a_contract(self):
        self.assertEqual(
            v("Engineering Coach",
              "This is a contract position in which coaches are paid per hour."), C)
        self.assertEqual(
            v("Product Engineering Manager",
              "This role is initially offered as a contract position with the "
              "potential to convert to full-time."), C)


class DescriptionsThatOnlyLookLikeContract(unittest.TestCase):
    """The 737 false positives, one per shape.

    Each string is a real description from the board and each role is
    permanent or unstated. A classifier reading loose words out of a
    description gets every one of these wrong.
    """

    def test_contract_as_a_customer_or_a_programme(self):
        for d in (
            "Support the Sustainment Logistics Maintenance (DSLM) contract, "
            "located in Colorado Springs.",
            "Federal Government contract labor categories and contract wage rates.",
            "Review control system specifications to ensure contract compliance.",
            "Deliver the solution to the contract schedule, costs and quality.",
        ):
            self.assertEqual(v("Software Engineering Manager", d), U, d[:40])

    def test_contractors_as_other_people(self):
        for d in (
            "Consistently ranked among the top 400 Contractors list.",
            "A trusted technical advisor to clients, contractors and suppliers.",
            "Privacy Notice for Employees, Contractors, Candidates and Visitors.",
        ):
            self.assertEqual(v("Senior Engineering Manager", d), U, d[:40])

    def test_contracts_as_an_engineering_word(self):
        for d in (
            "We write contracts before logic, test against real systems.",
            "Defining extension contracts, managing inbound requests.",
            "Low-latency execution services with clear contracts and retries.",
            "Experience supporting enterprise contracts and reseller billing.",
        ):
            self.assertEqual(v("Head of Engineering", d), U, d[:40])

    def test_per_day_is_throughput(self):
        # "3 trillion events per day" is not a day rate. This one mattered:
        # the phrase alone matched 47 roles and every one was a volume.
        for d in ("Processing almost 3 trillion events per day.",
                  "Products which consume billions of requests per day.",
                  "Domain experts are paid over $4 million per day."):
            self.assertEqual(v("Engineering Manager", d), U, d[:40])

    def test_temporary_is_us_benefits_boilerplate(self):
        for d in ("Regular employees are those who are not temporary, such as "
                  "interns. Temporary employees are eligible for paid sick time.",
                  "Individuals with temporary visas including F-1 and H-1B."):
            self.assertEqual(v("Engineering Manager", d), U, d[:40])

    def test_interim_in_a_description_is_not_the_job(self):
        for d in ("In the interim, this position will be onsite Tuesday.",
                  "Interim top secret clearance is required to start.",
                  "Reports to: Head of Engineering (interim Systems lead)."):
            self.assertEqual(v("Senior Engineering Manager", d), U, d[:40])

    def test_fractional_in_a_description_is_hardware(self):
        self.assertEqual(
            v("Head of Engineering, Compute",
              "GPU / accelerated compute: fractional GPUs (MIG, MPS, "
              "time-slicing), GPU scheduling."), U)

    def test_ftc_in_a_description_is_the_federal_trade_commission(self):
        # Every single occurrence of a bare "FTC" in 5,474 descriptions was
        # this paragraph. In a TITLE it is always a fixed-term contract, which
        # is why the two are read by different rules.
        for d in ("Learn more about avoiding job scams at "
                  "https://consumer.ftc.gov/consumer-alerts/2022/05/want-work-home",
                  "Report suspicious outreach to reportfraud.ftc.gov."):
            self.assertEqual(v("QA Engineering Manager", d), U, d[:40])

    def test_statement_of_work_is_agency_boilerplate(self):
        self.assertEqual(
            v("Platform Engineering Director",
              "No agency may submit candidates without a valid written "
              "Statement of Work in place for this position."), U)

    def test_independent_contractor_in_a_privacy_notice(self):
        # 20 hits in the corpus, 19 of them this sentence.
        self.assertEqual(
            v("Engineering Manager, Ads ML",
              "We use this information to evaluate your application for "
              "employment or an independent contractor role, as applicable."), U)


class Permanent(unittest.TestCase):
    def test_the_employer_saying_so(self):
        for d in ("This is a permanent position based in our London office.",
                  "Contract Type: Permanent. Hybrid Working: Hybrid",
                  "Contract: Permanent, full-time",
                  "Duration of Assignment: Permanent",
                  "Employment Type: Full time"):
            self.assertEqual(v("Director of Software Engineering", d), P, d[:40])

    def test_permanent_resident_is_immigration_not_employment(self):
        # The commonest use of the word in the whole corpus, and it says
        # nothing whatever about the role.
        for d in ("Applicants must be a US citizen or permanent resident.",
                  "You must be legally authorized to work in the United "
                  "States on a permanent basis.",
                  "Permanent work authorization is required."):
            self.assertEqual(v("Engineering Manager", d), U, d[:40])

    def test_permanent_equipment_repairs_are_not_a_job_offer(self):
        # A maintenance role describing repairs. Labelling it permanent is a
        # mislabel with a cost: a reader filtering to contract plus unstated
        # loses a role this module had no business having a view on.
        self.assertEqual(
            v("Operations Performance Engineering Manager",
              "Identify and drive temporary and permanent equipment repairs "
              "and improvements."), U)

    def test_may_convert_to_permanent_means_it_is_not_permanent_now(self):
        # The word is present and the role is a contract. Reading it without
        # the clause around it inverts the answer.
        self.assertEqual(
            v("Product Engineering Manager",
              "Offered as a contract position with the potential to convert "
              "to a permanent role."), C)


class UnstatedIsNotPermanent(unittest.TestCase):
    """The distinction the whole module turns on."""

    def test_silence_is_its_own_value(self):
        self.assertEqual(v("Engineering Manager",
                           "Lead a team of six. Own the roadmap."), U)

    def test_unstated_is_not_in_the_permanent_bucket(self):
        self.assertNotEqual(v("Engineering Manager", "Lead a team."), P)

    def test_the_three_values_are_the_declared_ones(self):
        for t, d in (("Engineering Manager", ""),
                     ("Interim CTO", ""),
                     ("Engineering Manager", "This is a permanent position.")):
            self.assertIn(v(t, d), employment.VALUES)


class TheTitleOutranksTheDescription(unittest.TestCase):
    def test_a_contract_title_survives_permanent_boilerplate(self):
        # Fixed-term adverts carry the same benefits paragraph as permanent
        # ones. The title is where the employer answers this on purpose.
        self.assertEqual(
            v("Engineering Manager (12 Month FTC)",
              "Permanent employees are eligible for the full benefits package. "
              "This is a permanent position within a growing team."), C)


class WhatThePlatformSaid(unittest.TestCase):
    """Six platforms answer this outright and nothing read any of them.

    Measured on 4 September 2026 by fetching one live board per platform:
    ashby 2,607 boards, workable 2,094, personio 1,258, recruitee 993,
    smartrecruiters 910, lever 65. That is 7,927 of 17,811 boards where the
    employer filled the field in and this tool guessed from prose instead.
    The first Workable board tried had 22 postings and 10 were typed
    "Contract"; every one was stored as "unstated".
    """

    def test_the_values_that_mean_contract(self):
        for v in ("Contract", "contract", "Temporary", "freelance",
                  "Fixed Term Contract", "FIXED_TERM", "contractor",
                  "contract_to_hire", "seasonal", "interim"):
            self.assertEqual(employment.from_platform(v), C, v)

    def test_the_values_that_mean_permanent(self):
        for v in ("permanent", "Permanent", "fulltime_permanent",
                  "parttime_permanent", "regular"):
            self.assertEqual(employment.from_platform(v), P, v)

    def test_a_schedule_is_not_a_contract_type(self):
        # THE trap in this table. "Full-time" answers a different question,
        # and a six month contract can be full time. Reading it as permanent
        # would hide contract roles behind a label asserting the opposite.
        for v in ("Full-time", "FullTime", "full time", "Part-time",
                  "PartTime", "Full time days",
                  "Full time, Part time, and Weekend shifts available."):
            self.assertEqual(employment.from_platform(v), U, v)

    def test_an_internship_is_not_what_a_contract_hunter_means(self):
        # Genuinely fixed-term, and putting it in the contract facet would
        # fill that facet with roles nobody searching it wants.
        for v in ("intern", "Intern", "internship", "trainee", "apprentice",
                  "working_student"):
            self.assertEqual(employment.from_platform(v), U, v)

    def test_smartrecruiters_sends_a_dict_and_the_id_is_the_machine_value(self):
        # The label has been through a translation table; the id has not.
        self.assertEqual(
            employment.from_platform({"id": "permanent", "label": "Full-time"}), P)
        self.assertEqual(
            employment.from_platform({"id": "contractor", "label": "Contractor"}), C)

    def test_the_four_spellings_of_two_words_agree(self):
        # Ashby "FullTime", Workable "Full-time", Lever "Full time",
        # Recruitee "fulltime_permanent". Same field, four vocabularies.
        self.assertEqual(employment.from_platform("FullTime"),
                         employment.from_platform("Full-time"))
        self.assertEqual(employment.from_platform("Full-time"),
                         employment.from_platform("full time"))

    def test_an_unknown_value_is_unstated_not_a_guess(self):
        # A platform that adds a value must not have it silently read as one
        # of the two that matter.
        for v in (None, "", "Nonsense", 7, [], {"label": "?"}):
            self.assertEqual(employment.from_platform(v), U, repr(v))


class WhoWinsWhenTheyDisagree(unittest.TestCase):
    """Title, then the platform's field, then the prose."""

    def _j(self, title, desc="", stated=U):
        j = Job(company="X", title=title, url="https://e.invalid/" + title[:8],
                platform="workable", location="London, UK", description=desc,
                employment=stated)
        enrich(j)
        return j.employment

    def test_the_platform_field_beats_silent_prose(self):
        self.assertEqual(self._j("Engineering Manager", "Lead a team.",
                                 stated=C), C)

    def test_a_contract_title_beats_a_permanent_platform_field(self):
        # An employer who leaves the dropdown on its default while titling
        # the role "Interim Head of Engineering" has told you which they
        # meant. The title is the deliberate act.
        self.assertEqual(self._j("Interim Head of Engineering", "x", stated=P), C)
        self.assertEqual(self._j("Engineering Manager (12 Month FTC)", "x",
                                 stated=P), C)

    def test_the_prose_runs_only_where_the_platform_said_nothing(self):
        self.assertEqual(self._j("Engineering Manager",
                                 "This is a permanent position."), P)
        self.assertEqual(self._j("Engineering Manager",
                                 "Contract Type: Fixed Term Contract"), C)

    def test_nothing_anywhere_is_still_unstated(self):
        self.assertEqual(self._j("Engineering Manager", "Lead a team."), U)

    def test_a_platform_value_is_flagged_as_the_employers_own_words(self):
        j = Job(company="X", title="Engineering Manager", url="https://e.invalid/z",
                platform="workable", location="London, UK",
                description="Lead a team.", employment=C)
        enrich(j)
        self.assertTrue([f for f in j.flags if "stated by the employer" in f])


class AdaptersReadTheField(unittest.TestCase):
    """The bug was not the mapping, it was that nobody called it."""

    def _fixture(self, name):
        import json
        return json.loads(
            (Path(__file__).parent / "fixtures" / name).read_text(encoding="utf-8"))

    def test_workable_reads_employment_type(self):
        from jobradar.adapters import platforms
        from jobradar.models import Source
        src = Source(company="Acme", url="https://apply.workable.com/api/v1/"
                     "widget/accounts/acme", platform="workable", country="UK")
        jobs = list(platforms.parse_workable(self._fixture("workable_company.json"), src))
        self.assertTrue(jobs)
        # The fixture carries Full-time and Part-time, both of which are
        # schedules, so every row is unstated rather than permanent.
        self.assertEqual({j.employment for j in jobs}, {U})

    def test_a_workable_contract_posting_is_read_as_one(self):
        from jobradar.adapters import platforms
        from jobradar.models import Source
        src = Source(company="Acme", url="https://apply.workable.com/api/v1/"
                     "widget/accounts/acme", platform="workable", country="UK")
        payload = {"jobs": [{"title": "Engineering Manager", "url":
                             "https://apply.workable.com/acme/j/ABC/",
                             "employment_type": "Contract",
                             "description": "Lead a team."}]}
        job = list(platforms.parse_workable(payload, src))[0]
        self.assertEqual(job.employment, C)

    def test_personio_reads_employment_type_and_not_schedule(self):
        # Personio sends BOTH <employmentType>permanent</employmentType> and
        # <schedule>full-time</schedule>. Reading the second would answer a
        # different question.
        from jobradar.adapters import platforms
        from jobradar.models import Source
        src = Source(company="Acme", url="https://acme.jobs.personio.de/xml",
                     platform="personio", country="UK")
        xml = ("<positions><position><id>1</id><name>Engineering Manager</name>"
               "<office>London</office><employmentType>freelance</employmentType>"
               "<schedule>full-time</schedule><description>x</description>"
               "</position></positions>")
        job = list(platforms.parse_personio(xml, src))[0]
        self.assertEqual(job.employment, C)

    def test_every_platform_with_the_field_actually_calls_the_mapper(self):
        # A row count would have passed while the column stayed empty, which
        # is this repo's signature failure. Assert the call site exists for
        # each platform the live probe found the field on.
        src = (Path(__file__).parent.parent / "jobradar" / "adapters"
               / "platforms.py").read_text(encoding="utf-8")
        import ast
        tree = ast.parse(src)
        wired = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if not node.name.startswith("parse_"):
                continue
            body = ast.dump(node)
            if "from_platform" in body:
                wired.add(node.name.removeprefix("parse_"))
        for platform in ("ashby", "lever", "workable", "smartrecruiters",
                         "recruitee", "personio"):
            self.assertIn(platform, wired,
                          f"parse_{platform} does not read the employment field")


class Flag(unittest.TestCase):
    def test_only_contract_roles_get_one(self):
        self.assertEqual(employment.flag(P, "permanent"), "")
        self.assertEqual(employment.flag(U, ""), "")

    def test_the_contract_flag_names_the_evidence(self):
        f = employment.flag(C, "12 Month FTC")
        self.assertIn("contract", f.lower())
        self.assertIn("12 Month FTC", f)


class WiredIntoTheePipeline(unittest.TestCase):
    """`enrich` is what fills the column the dashboard filters on."""

    def _job(self, title, desc=""):
        return Job(company="X", title=title, url="https://e.invalid/1",
                   platform="greenhouse", location="London, UK", description=desc)

    def test_enrich_sets_the_field(self):
        j = self._job("Interim Head of Engineering")
        enrich(j)
        self.assertEqual(j.employment, C)

    def test_enrich_flags_a_contract_role(self):
        j = self._job("Engineering Manager (12 Month FTC)")
        enrich(j)
        self.assertTrue([f for f in j.flags if "contract or interim" in f])

    def test_enrich_does_not_flag_a_permanent_role(self):
        j = self._job("Engineering Manager", "This is a permanent position.")
        enrich(j)
        self.assertEqual([f for f in j.flags if "contract or interim" in f], [])

    def test_a_job_defaults_to_unstated_not_permanent(self):
        self.assertEqual(Job(company="X", title="T", url="u",
                             platform="p").employment, U)

    def test_enrich_never_drops_a_role(self):
        # Employment type is a fact for the reader to act on, not a gate. A
        # filter here would hide the roles this was built to surface.
        j = self._job("Engineering Manager (12 Month FTC)")
        self.assertIs(enrich(j), j)


class StoredAndReadBack(unittest.TestCase):
    def setUp(self):
        from jobradar import store
        self.store = store
        self.con = store.connect(":memory:")

    def tearDown(self):
        self.con.close()

    def test_the_column_round_trips(self):
        j = Job(company="Acme", title="Engineering Manager (12 Month FTC)",
                url="https://e.invalid/a", platform="greenhouse",
                location="London, UK")
        enrich(j)
        self.store.upsert_roles(self.con, [j])
        row = self.con.execute("SELECT employment FROM roles WHERE uid=?",
                               (j.uid,)).fetchone()
        self.assertEqual(row["employment"], C)

    def test_an_unclassified_rescan_does_not_wipe_a_classification(self):
        # LinkedIn, Workday and SmartRecruiters send no description on the
        # list endpoint, so the next scan pass sees a role it cannot classify.
        # "I could not see" must not overwrite "I saw, and it is a contract".
        j = Job(company="Acme", title="Head of Engineering",
                url="https://e.invalid/b", platform="linkedin",
                location="London, UK",
                description="Contract Type: Fixed Term Contract")
        enrich(j)
        self.store.upsert_roles(self.con, [j])

        bare = Job(company="Acme", title="Head of Engineering",
                   url="https://e.invalid/b", platform="linkedin",
                   location="London, UK", description="")
        enrich(bare)
        self.assertEqual(bare.employment, U)
        self.store.upsert_roles(self.con, [bare])

        row = self.con.execute("SELECT employment FROM roles WHERE uid=?",
                               (j.uid,)).fetchone()
        self.assertEqual(row["employment"], C)

    def test_a_row_written_before_the_column_existed_reads_as_unstated(self):
        # Never as permanent. That would be the tool inventing a fact about
        # somebody's job out of its own migration history.
        self.con.execute(
            "INSERT INTO roles (uid,company,title,url,first_seen,last_seen) "
            "VALUES ('old','Acme','Engineering Manager','u','2026-01-01',"
            "'2026-01-01')")
        row = self.con.execute("SELECT employment FROM roles WHERE uid='old'"
                               ).fetchone()
        self.assertEqual(row["employment"], U)


class RescreenKeepsWhatOnlyAScanCanKnow(unittest.TestCase):
    """The platform's field lives in the column and nowhere else.

    `rescreen` rebuilds a Job from the stored row and re-derives the columns.
    Building it with the default "unstated" and re-running the text
    classifier discarded the employer's own answer and replaced it with a
    guess: a Workable posting typed "Contract", whose description says
    nothing about terms, was silently downgraded by the command whose job is
    to refresh it. Only a fresh scan can recover it, so nothing here may
    throw it away.
    """

    def test_the_command_carries_the_stored_value_into_the_job(self):
        import ast
        src = (Path(__file__).parent.parent / "jobradar" / "cli.py").read_text(
            encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "cmd_rescreen")
        # The Job(...) built from the row must pass `employment`. Without it
        # the field defaults to "unstated" and enrich overwrites the column.
        calls = [n for n in ast.walk(fn)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id == "Job"]
        self.assertTrue(calls, "cmd_rescreen no longer builds a Job")
        for c in calls:
            self.assertIn("employment", [k.arg for k in c.keywords],
                          "cmd_rescreen builds a Job without carrying "
                          "employment, so a platform-stated value is lost")

    def test_the_row_is_selected_so_there_is_something_to_carry(self):
        src = (Path(__file__).parent.parent / "jobradar" / "cli.py").read_text(
            encoding="utf-8")
        import ast
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "cmd_rescreen")
        sql = " ".join(n.value for n in ast.walk(fn)
                       if isinstance(n, ast.Constant) and isinstance(n.value, str))
        self.assertIn("r.employment", sql,
                      "cmd_rescreen does not select the employment column")


class Dashboard(unittest.TestCase):
    def test_a_row_missing_the_column_is_unstated_not_permanent(self):
        from jobradar.output import interactive
        self.assertEqual(interactive._emp({}), U)

    def test_an_unknown_value_is_unstated(self):
        from jobradar.output import interactive

        class Row(dict):
            def __getitem__(self, k):
                return "somethingelse"

        self.assertEqual(interactive._emp(Row()), U)

    def test_every_value_has_a_label(self):
        from jobradar.output import interactive
        for value in employment.VALUES:
            self.assertIn(value, interactive._EMP_LABELS)


class BundledContractSources(unittest.TestCase):
    def test_the_source_list_carries_a_contract_scoped_search(self):
        import json
        rows = json.loads(
            (Path(__file__).parent.parent / "sources" / "sources.json")
            .read_text(encoding="utf-8"))["sources"]
        li = [r for r in rows if r.get("platform") == "linkedin"]
        self.assertTrue([r for r in li if "f_JT=C" in r["url"]],
                        "no contract-scoped LinkedIn search in the source list")

    def test_the_contract_searches_are_keyword_templates(self):
        # Scoped by job type, filled with the reader's OWN titles. A source
        # carrying somebody else's search terms is that person's search saved
        # as an employer list.
        import json
        rows = json.loads(
            (Path(__file__).parent.parent / "sources" / "sources.json")
            .read_text(encoding="utf-8"))["sources"]
        for r in rows:
            if r.get("platform") == "linkedin" and "f_JT=" in r["url"]:
                self.assertTrue(r.get("keyword_template"), r["company"])
                self.assertIn("{keyword}", r["url"])


if __name__ == "__main__":
    unittest.main()
