"""No test may write into the developer's own state directory.

Found on 7 September 2026, and only by accident. A scan had been given a
resume checkpoint at `state/scan-progress.json`; it kept vanishing mid-run.
The cause was the test suite: three tests invoked `cmd_scan` without
`--state`, `args.state` was None, `State(None)` falls back to
`state/seen.json` RELATIVE TO THE WORKING DIRECTORY, and the suite runs from
the repo root.

So those tests had been overwriting the real seen-set on every run since they
were written, which is a quiet corruption of the one file that decides what
counts as a new role. Nothing failed. Nobody noticed. It only surfaced because
scans learned to checkpoint and the suite started deleting the checkpoint too,
turning a silent write into a visible disappearance.

The lesson generalises past `--state`, so this checks the shape rather than
the three tests: any test that runs a scan has to say where its state goes.
"""

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

HERE = Path(__file__).resolve().parent

# `--db :memory:` is not enough on its own. The database and the seen-set are
# separate files with separate defaults, and only one of them was ever passed.
STATE_FLAG = "--state"


def _scan_invocations(tree):
    """Every call that starts a scan through the CLI, with its argv list."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = ""
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name not in ("main", "cmd_scan"):
            continue
        for arg in node.args:
            if not isinstance(arg, ast.List):
                continue
            words = [e.value for e in arg.elts if isinstance(e, ast.Constant)]
            if "scan" in words:
                out.append((node.lineno, words))
    return out


class NoTestScansIntoTheRealStateDirectory(unittest.TestCase):

    def test_every_cli_scan_in_the_suite_names_its_state_file(self):
        offenders = []
        for path in sorted(HERE.glob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for lineno, words in _scan_invocations(tree):
                if STATE_FLAG not in words:
                    offenders.append(f"{path.name}:{lineno}")
        self.assertEqual(
            offenders, [],
            "these run a scan without --state, so they write the real "
            "state/seen.json in the repo root: " + ", ".join(offenders))

    def test_the_guard_can_actually_see_a_scan_call(self):
        # A checker that matches nothing passes for ever and guards nothing.
        found = 0
        for path in sorted(HERE.glob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            found += len(_scan_invocations(tree))
        self.assertGreater(found, 0, "this guard is watching nothing")

    def test_it_would_catch_a_scan_with_no_state_flag(self):
        tree = ast.parse('main(["-c", "x", "scan", "--no-open"])')
        found = _scan_invocations(tree)
        self.assertEqual(len(found), 1)
        self.assertNotIn(STATE_FLAG, found[0][1])

    def test_it_does_not_flag_a_scan_that_does_name_one(self):
        tree = ast.parse('main(["-c", "x", "scan", "--state", "/tmp/s.json"])')
        self.assertIn(STATE_FLAG, _scan_invocations(tree)[0][1])


class TheDefaultIsRelativeWhichIsWhyThisMatters(unittest.TestCase):
    def test_state_with_no_path_lands_in_the_working_directory(self):
        # The behaviour that made the above possible. Documented here so the
        # next person to read `State(None)` knows what it costs in a test.
        from jobradar.state import State, DEFAULT_DIR
        self.assertEqual(Path(State(None).path).parent, DEFAULT_DIR)
        self.assertFalse(Path(DEFAULT_DIR).is_absolute())


if __name__ == "__main__":
    unittest.main()
