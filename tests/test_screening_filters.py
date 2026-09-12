"""Every dealbreaker reaches the screen, or the screen is not a screen.

The screening prompt pasted the raw config YAML and cut it at 6,000
characters. The live config is 9,941 after redaction, so 3,941 characters went
missing from the end of every screening prompt this tool has ever built, and
what lives at the end of a config file is the dealbreakers.

Measured on 12 September 2026 against the real config: of five dealbreakers,
`solutions-architect wording` and `manages managers` were never sent. The
second is the one that matters most to this user, and it had never reached a
single screen.

Nothing announced it. The screening still produced its verdict, its fails
table and its gaps, and read exactly like one that had checked everything. Two
screens noticed only because the YAML happened to end mid-sentence and the
model remarked on it unprompted. Had the cut landed on a line boundary,
nothing anywhere would have said so, which is this repo's signature failure
wearing a byte count.
"""

import sys
import unittest
from pathlib import Path
from tempfile import mkdtemp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar.runner import screening_filters

MANY = 40


def _config(n_dealbreakers=MANY, padding_comment_kb=8):
    """A config big enough that any byte cap would bite, with the
    dealbreakers last, where they really live."""
    d = Path(mkdtemp())
    cv = d / "cv.txt"
    cv.write_text("Engineering manager. Led a team of six.", encoding="utf-8")
    pad = "\n".join(f"# {'x' * 70}" for _ in range(padding_comment_kb * 14))
    db = "\n".join(
        f"  - name: rule {i}\n    pattern: 'pattern number {i}'\n"
        f"    hard: {'true' if i % 2 else 'false'}"
        for i in range(n_dealbreakers))
    (d / "config.yaml").write_text(
        "titles:\n  include:\n    - engineering manager\n"
        f"cv:\n  path: {cv}\n"
        "salary:\n  floor: 140000\n  currency: GBP\n"
        "locations:\n  countries: [UK]\n"
        f"{pad}\n"
        f"dealbreakers:\n{db}\n",
        encoding="utf-8")
    return d / "config.yaml"


class NoDealbreakerIsLost(unittest.TestCase):
    def setUp(self):
        self.path = _config()
        self.out = screening_filters(self.path, str(self.path))

    def test_the_last_one_is_present(self):
        # The end of the list is exactly what a byte cap removes.
        self.assertIn(f"rule {MANY - 1}", self.out)

    def test_every_one_is_present(self):
        missing = [i for i in range(MANY) if f"rule {i}" not in self.out]
        self.assertEqual(missing, [], f"dealbreakers missing: {missing}")

    def test_the_count_is_stated_so_it_can_be_checked(self):
        # A reader can compare this against the table the screen produces.
        self.assertIn(f"all {MANY} of them", self.out)

    def test_hard_and_soft_are_distinguished(self):
        self.assertIn("(hard)", self.out)
        self.assertIn("(soft)", self.out)

    def test_the_pattern_is_sent_not_just_the_name(self):
        # A name without its regex cannot be matched against the posting.
        self.assertIn("pattern number 0", self.out)


class TheRealConfigSurvives(unittest.TestCase):
    """The specific regression, against the file it happened to."""

    def _real(self):
        root = Path(__file__).resolve().parent.parent
        for name in ("config.local.yaml", "config.yaml"):
            if (root / name).exists():
                return root / name
        return None

    def test_every_dealbreaker_in_the_shipped_config_is_sent(self):
        from jobradar.config import load
        path = self._real()
        if path is None:
            self.skipTest("no config in this checkout")
        try:
            cfg = load(str(path))
        except Exception:
            self.skipTest("config does not load in this checkout")
        out = screening_filters(path, str(path))
        for d in cfg.dealbreakers:
            self.assertIn(d.name, out,
                          f"{d.name} never reaches the screening prompt")

    def test_it_is_smaller_than_the_truncated_yaml_it_replaced(self):
        # Complete AND shorter: comments and seventeen thousand source rows
        # are not filters.
        path = self._real()
        if path is None:
            self.skipTest("no config in this checkout")
        self.assertLess(len(screening_filters(path, str(path))), 6000)


class AConfigThatWillNotLoadSaysSo(unittest.TestCase):
    """Rather than sending a partial set that reads as the whole one, which
    is the same failure by another route."""

    def test_a_broken_config_is_reported_not_papered_over(self):
        d = Path(mkdtemp())
        bad = d / "config.yaml"
        bad.write_text("titles: [unclosed\n", encoding="utf-8")
        out = screening_filters(bad, str(bad))
        self.assertIn("could not be read", out)

    def test_it_tells_the_screen_not_to_imply_a_pass(self):
        d = Path(mkdtemp())
        bad = d / "config.yaml"
        bad.write_text("::: not yaml :::\n", encoding="utf-8")
        out = screening_filters(bad, str(bad))
        self.assertIn("do not imply any dealbreaker passed", out)

    def test_a_missing_file_does_not_raise(self):
        out = screening_filters(Path(mkdtemp()) / "nope.yaml",
                                str(Path(mkdtemp()) / "nope.yaml"))
        self.assertIn("could not be read", out)


class TheOtherFiltersAreThereToo(unittest.TestCase):
    def setUp(self):
        self.path = _config()
        self.out = screening_filters(self.path, str(self.path))

    def test_the_salary_floor(self):
        self.assertIn("140,000 GBP", self.out)

    def test_the_countries(self):
        self.assertIn("UK", self.out)

    def test_the_titles(self):
        self.assertIn("engineering manager", self.out)


class TheActualPromptCarriesThemAll(unittest.TestCase):
    """The end-to-end check, and the one that would have caught this.

    The tests above call `screening_filters` directly, so they still pass
    against the truncating version: the function exists, it is simply not
    what the prompt was built from. That is the same shape as the bug, a
    thing that looks checked and is not, so this asserts on the string the
    model is actually handed.
    """

    def test_every_dealbreaker_appears_in_the_screening_prompt(self):
        from jobradar.runner import build_prompt
        path = _config()
        filters = screening_filters(path, str(path))
        prompt = build_prompt("screen", filters, "source-cv.txt")
        missing = [i for i in range(MANY) if f"rule {i}" not in prompt]
        self.assertEqual(missing, [],
                         f"the screening prompt is missing dealbreakers: "
                         f"{missing}")

    def test_the_real_configs_last_dealbreaker_reaches_the_prompt(self):
        # `manages managers` is the one that had never been sent.
        from jobradar.config import load
        from jobradar.runner import build_prompt
        root = Path(__file__).resolve().parent.parent
        path = next((root / n for n in ("config.local.yaml", "config.yaml")
                     if (root / n).exists()), None)
        if path is None:
            self.skipTest("no config in this checkout")
        try:
            cfg = load(str(path))
        except Exception:
            self.skipTest("config does not load in this checkout")
        if not cfg.dealbreakers:
            self.skipTest("no dealbreakers configured")
        prompt = build_prompt("screen", screening_filters(path, str(path)),
                              "source-cv.txt")
        last = cfg.dealbreakers[-1].name
        self.assertIn(last, prompt,
                      f"the last dealbreaker, {last!r}, does not reach the "
                      f"screening prompt")


class NoByteCapRemains(unittest.TestCase):
    def test_the_prompt_builder_does_not_slice_the_config(self):
        """Parsed, not grepped for an absent string.

        The old code was `redact_secrets(...)[:6000]`. This checks the call
        site builds its filters through the function that cannot truncate,
        rather than searching the file for a number that a comment explaining
        the bug would also contain.
        """
        import ast
        src = (Path(__file__).resolve().parent.parent / "jobradar"
               / "runner.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "screening_filters"]
        self.assertTrue(calls, "the screening prompt no longer builds filters "
                               "through screening_filters")


if __name__ == "__main__":
    unittest.main()
