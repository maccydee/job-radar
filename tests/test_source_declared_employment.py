"""A source whose QUERY already knows the answer.

Reed's search API filters at the request and sends no per-result flag, so for
a `contract=true` search the only place the employment type exists is the URL.
Measured live on 6 September 2026 for "engineering manager":

    contract=true   418 results
    permanent=true  3,584 results
    unfiltered      4,112 results
    overlap between the two filtered pages: 0

The text classifier reading the same 100 known-contract rows labelled 57 of
them, missed 42, and called one permanent. A wrong "permanent" on a contract
role is the failure this repo is named for: it renders exactly like a right
one, and the reader hunting contract work never sees the role.

The counterweight, and the reason `Source.employment` is guarded rather than
free text: two LinkedIn sources were added and removed on 4 September for
asserting a job-type scope the endpoint ignored. A source that lies here
mislabels every posting it returns.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import adapters, employment, sources as src_mod
from jobradar.config import ConfigError, _extra_sources
from jobradar.models import Job, Source

C, P, U = employment.CONTRACT, employment.PERMANENT, employment.UNSTATED

REED_URL = ("https://www.reed.co.uk/api/1.0/search"
            "?keywords={keyword}&contract=true&permanent=false")


def _payload(*titles):
    return {"results": [
        {"jobId": 1000 + i, "jobTitle": t, "employerName": "An Agency",
         "locationName": "London", "jobDescription": "Lead a team of six.",
         "date": "01/09/2026", "jobUrl": f"https://www.reed.co.uk/jobs/{1000+i}"}
        for i, t in enumerate(titles)]}


class TheDeclarationReachesTheJobs(unittest.TestCase):
    def test_a_declared_source_labels_what_it_returns(self):
        src = Source(company="Reed contract", url=REED_URL.format(keyword="x"),
                     platform="reed", country="UK", employment=C)
        jobs = adapters.parse(_payload("Engineering Manager",
                                       "Delivery Manager"), src)
        self.assertTrue(jobs)
        self.assertEqual({j.employment for j in jobs}, {C})

    def test_an_undeclared_source_leaves_it_alone(self):
        src = Source(company="Reed", url=REED_URL.format(keyword="x"),
                     platform="reed", country="UK")
        jobs = adapters.parse(_payload("Engineering Manager"), src)
        self.assertEqual(jobs[0].employment, U)

    def test_a_per_result_platform_value_still_wins(self):
        # The declaration is a filter on a URL; a field the platform actually
        # sent is better evidence, so it must not be overwritten.
        src = Source(company="Acme", url="https://apply.workable.com/api/v1/"
                     "widget/accounts/acme", platform="workable", country="UK",
                     employment=C)
        payload = {"jobs": [{"title": "Engineering Manager",
                             "url": "https://apply.workable.com/acme/j/A/",
                             "employment_type": "permanent",
                             "description": "Lead a team."}]}
        self.assertEqual(adapters.parse(payload, src)[0].employment, P)


class TheDeclarationSurvivesKeywordExpansion(unittest.TestCase):
    """Found by measuring, not by reading.

    `expand_templates` builds one Source per title and rebuilds it field by
    field. It already had this exact bug once, for `keyword_template`, with a
    comment beside it saying so. `employment` was dropped the same way, so a
    Reed contract search arrived unscoped and 100 postings known to be
    contract work were classified from their prose instead.
    """

    def test_every_expanded_search_inherits_it(self):
        template = Source(company="Reed contract", url=REED_URL,
                          platform="reed", country="UK",
                          keyword_template=True, employment=C)
        out = src_mod.expand_templates(
            [template], ["engineering manager", "head of engineering"],
            countries=[])
        self.assertEqual(len(out), 2)
        for s in out:
            self.assertEqual(s.employment, C, s.company)
            self.assertTrue(s.keyword_template, s.company)

    def test_the_expansion_still_carries_the_other_fields(self):
        template = Source(company="Reed contract", url=REED_URL,
                          platform="reed", country="UK", sector="technology",
                          keyword_template=True, employment=C)
        s = src_mod.expand_templates([template], ["engineering manager"],
                                     countries=[])[0]
        self.assertEqual((s.platform, s.country, s.sector),
                         ("reed", "UK", "technology"))


class TheDeclarationRoundTrips(unittest.TestCase):
    def test_to_dict_and_back(self):
        s = Source(company="R", url="https://x.invalid/a", platform="reed",
                   employment=C)
        self.assertEqual(Source.from_dict(s.to_dict()).employment, C)

    def test_an_undeclared_source_writes_no_key(self):
        # An empty `employment: ""` in the bundled list would read as a
        # declaration nobody made.
        s = Source(company="R", url="https://x.invalid/a", platform="reed")
        self.assertNotIn("employment", s.to_dict())


class TheConfigRefusesAGuess(unittest.TestCase):
    """Every posting the source returns is labelled with this, so a typo is
    not a typo, it is a few hundred wrong rows."""

    def _entry(self, **kw):
        d = {"company": "Reed contract", "url": REED_URL,
             "platform": "reed", "keyword_template": True}
        d.update(kw)
        return d

    def test_a_real_value_is_accepted(self):
        out = _extra_sources([self._entry(employment="contract")], "sources.extra")
        self.assertEqual(out[0]["employment"], C)

    def test_case_and_whitespace_are_forgiven(self):
        out = _extra_sources([self._entry(employment="  Contract ")],
                             "sources.extra")
        self.assertEqual(out[0]["employment"], C)

    def test_a_typo_is_refused_rather_than_ignored(self):
        for bad in ("contarct", "perm", "full-time", "yes", "", 1, None, True):
            with self.assertRaises(ConfigError, msg=repr(bad)):
                _extra_sources([self._entry(employment=bad)], "sources.extra")

    def test_the_error_says_what_is_at_stake(self):
        try:
            _extra_sources([self._entry(employment="contarct")], "sources.extra")
        except ConfigError as e:
            self.assertIn("every posting", str(e).lower())
        else:
            self.fail("a bad employment value was accepted")


class TheBundledListDeclaresNothing(unittest.TestCase):
    def test_no_shipped_source_asserts_an_unverified_filter(self):
        # The bundled list goes to everybody, and nobody but the person who
        # measured the filter can know whether it filters. Declarations belong
        # in a personal config until somebody has checked.
        import json
        rows = json.loads(
            (Path(__file__).parent.parent / "sources" / "sources.json")
            .read_text(encoding="utf-8"))["sources"]
        declared = [r["company"] for r in rows if r.get("employment")]
        self.assertEqual(declared, [], f"undeclared filters shipped: {declared}")


class TheSetupWizardAsksForKeys(unittest.TestCase):
    """Callum, 6 Sept 2026: the wizard should ask for the keys a source needs.

    Two adapters here are keyed and neither was mentioned in the documented
    first step, so the only way to discover Reed existed was to add a source
    by hand and read the 401.
    """

    def test_both_keyed_sources_are_offered(self):
        from jobradar import setup_wizard as w
        labels = {e[0] for e in w.API_KEY_SOURCES}
        self.assertEqual(labels, {"Reed", "Adzuna"})

    def test_every_field_named_is_a_real_config_field(self):
        from jobradar import setup_wizard as w
        from jobradar.config import Config
        for _label, _url, _why, fields in w.API_KEY_SOURCES:
            for field, _prompt in fields:
                self.assertTrue(hasattr(Config, field),
                                f"{field} is not on Config")

    def test_adzuna_asks_for_both_halves(self):
        # One of two is a 401 later whose message cannot say which half is
        # missing.
        from jobradar import setup_wizard as w
        adzuna = next(e for e in w.API_KEY_SOURCES if e[0] == "Adzuna")
        self.assertEqual({f for f, _ in adzuna[3]},
                         {"adzuna_app_id", "adzuna_app_key"})

    def test_a_key_given_at_setup_is_written_and_read_back(self):
        import tempfile
        from jobradar import setup_wizard as w
        from jobradar.config import load
        cv = Path(tempfile.mkdtemp()) / "cv.txt"
        cv.write_text("x", encoding="utf-8")
        a = dict(w.DEFAULTS)
        a.update(cv_path=str(cv), titles_include=["engineering manager"],
                 reed_api_key="abc-123", adzuna_app_id="id1",
                 adzuna_app_key="k1")
        p = Path(tempfile.mkdtemp()) / "config.yaml"
        w.write_config(p, a)
        cfg = load(str(p))
        self.assertEqual(cfg.reed_api_key, "abc-123")
        self.assertEqual((cfg.adzuna_app_id, cfg.adzuna_app_key), ("id1", "k1"))

    def test_skipping_writes_no_empty_key_line(self):
        # An empty `reed_api_key:` reads as a configured source that is
        # failing, rather than one nobody turned on.
        import tempfile
        from jobradar import setup_wizard as w
        cv = Path(tempfile.mkdtemp()) / "cv.txt"
        cv.write_text("x", encoding="utf-8")
        a = dict(w.DEFAULTS)
        a.update(cv_path=str(cv), titles_include=["engineering manager"])
        p = Path(tempfile.mkdtemp()) / "config.yaml"
        w.write_config(p, a)
        text = p.read_text(encoding="utf-8")
        self.assertNotIn("reed_api_key", text)
        self.assertNotIn("adzuna_app_id", text)


class NoCredentialIsCommitted(unittest.TestCase):
    def test_the_personal_config_pattern_is_gitignored(self):
        # The key lives in config.local.yaml and this repo is public.
        ignore = (Path(__file__).parent.parent / ".gitignore").read_text(
            encoding="utf-8")
        self.assertIn("*.local.yaml", ignore)

    def test_no_tracked_file_sets_a_key_to_a_real_value(self):
        """A key field with something after the colon, anywhere that ships.

        Deliberately NOT "does any tracked file contain a UUID". That was the
        first version and it failed immediately on
        `jobs-page-4dc2685b-eb82-46d1-a3f9-1f0764dba814`, which is The L
        Suite's public Ashby board slug and not a secret at all. A test that
        cries wolf on every board with a UUID in its URL gets switched off,
        and then it is not guarding anything. The risk is an assignment, so
        the assertion is about assignments.
        """
        import re
        root = Path(__file__).parent.parent
        assigned = re.compile(
            r"(reed_api_key|adzuna_app_id|adzuna_app_key)\s*[:=]\s*"
            r"(?!['\"]?\s*(?:$|#|\{|<|\n))['\"]?([^\s'\"#,}]+)",
            re.I | re.M)
        for rel in ("sources/sources.json", "config.yaml",
                    "config.example.yaml", "README.md", "docs/SOURCES.md",
                    "docs/CONFIG.md", "docs/PLATFORMS.md"):
            f = root / rel
            if not f.exists():
                continue
            found = [m.group(0) for m in assigned.finditer(
                f.read_text(encoding="utf-8"))
                # The docs name the env vars and show placeholders. A value
                # that is obviously a placeholder is the documentation doing
                # its job.
                if not re.search(r"your|xxx|abc|<|\$|example|placeholder",
                                 m.group(2), re.I)]
            self.assertEqual(found, [], f"{rel} appears to set a real key")


if __name__ == "__main__":
    unittest.main()
