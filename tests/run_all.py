"""Run every test file in this directory, with nothing installed but the tool.

CI ran `python tests/test_core.py` and nothing else. That is not a suite, it
is one file, and the second file went unrun from the day it was added:
tests/test_locations.py held 47 tests, including every country-code rule that
decides whether a job is one the user can legally take, and CI had never
executed one of them. It could not have: it has no `sys.path` insert and no
runner block, so `python tests/test_locations.py` died on `ModuleNotFoundError`.

This is the second time the same shape has bitten. The first was a `__main__`
block sitting halfway up test_core.py, which meant CI ran 116 of 239 tests and
reported a full pass. Both failures share a cause: which tests run was decided
by hand somewhere, and the hand went out of date.

So this discovers them instead. Add a file called `test_*.py` to this
directory and it runs, in CI, on the next push, without anybody remembering to
add a line to a workflow.

pytest runs these too and does not need this file. It exists so the suite
stays runnable with no dependency beyond the package itself, which is what
lets somebody check the salary and location logic without installing anything.
"""

from __future__ import annotations

import importlib.util
import sys
import traceback
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = mod
    spec.loader.exec_module(mod)
    return mod


def _collect(mod) -> list[tuple[str, object]]:
    """Module-level `test_*` functions, and unittest.TestCase methods.

    The house style here is plain functions, but unittest is what most people
    reach for, and a TestCase file that silently runs nothing is worse than
    one that fails to import.
    """
    out = [(n, f) for n, f in sorted(vars(mod).items())
           if n.startswith("test_") and callable(f)
           and not isinstance(f, type)]
    for cname, cls in sorted(vars(mod).items()):
        if not (isinstance(cls, type) and issubclass(cls, unittest.TestCase)
                and cls is not unittest.TestCase):
            continue
        for mname in sorted(dir(cls)):
            if mname.startswith("test"):
                out.append((f"{cname}.{mname}", _as_callable(cls, mname)))
    return out


def _as_callable(cls, mname):
    """Run one TestCase method with its setUp/tearDown, raising on failure.

    `TestCase.run` swallows the exception into a result object, so calling it
    directly would report every test as passing. `debug()` lets it out.
    """
    return lambda: cls(mname).debug()


def run_one(fn):
    """Run one test. Returns ("pass"|"skip"|"fail", reason).

    `skip` exists because `debug()` does not handle skips the way a normal
    unittest runner does: `self.skipTest(...)` raises `unittest.SkipTest`,
    which arrived here as an ordinary exception and was reported as a
    failure. Three tests that skip when there is no config in the checkout
    turned every CI run red from 12 September 2026, while the same suite
    passed on a machine that had one. A skip is not a pass either, so it is
    counted and named separately rather than folded into the total.
    """
    try:
        fn()
        return "pass", ""
    except KeyboardInterrupt:
        raise
    except unittest.SkipTest as e:
        return "skip", str(e)
    except BaseException:
        # BaseException, not Exception. SystemExit is not an Exception, so a
        # test that exits walked straight past this and took the whole runner
        # with it: the log stopped mid-suite, with no FAIL line, no traceback
        # and no summary, and the job reported exit code 1 with nothing to say
        # why. It cost three red CI runs to find, and the test that did it was
        # one calling `rank.rank`, which checks for the `claude` binary up
        # front and raises SystemExit when it is missing.
        return "fail", ""


def main() -> int:
    files = sorted(p for p in HERE.glob("test_*.py"))
    if not files:
        print("no test files found, which is itself a failure")
        return 1

    total = bad = 0
    skipped = []
    unimportable = []
    for path in files:
        print(f"\n{path.name}")
        try:
            mod = _load(path)
        except Exception:
            # A file that will not import is a failure, not a file to skip.
            # Skipping is how the first one went missing.
            print(f"  FAIL  could not import {path.name}")
            traceback.print_exc()
            unimportable.append(path.name)
            bad += 1
            total += 1
            continue
        fns = _collect(mod)
        if not fns:
            # A test file with nothing runnable in it is a failure, not a
            # quiet zero. tests/test_scan_open.py was written as
            # unittest.TestCase classes, which this runner did not look for:
            # the file appeared in the listing, contributed no tests, and the
            # suite still printed a full pass. That is the same shape the
            # runner was written to stop.
            print(f"  FAIL  {path.name} defines no runnable tests")
            bad += 1
            total += 1
            continue
        for name, fn in fns:
            total += 1
            verdict, why = run_one(fn)
            if verdict == "pass":
                print(f"  pass  {name}")
            elif verdict == "skip":
                skipped.append((name, why))
                print(f"  skip  {name}: {why}")
            else:
                bad += 1
                print(f"  FAIL  {name}")
                traceback.print_exc()

    # Named again at the end, where a reader of a two thousand line log
    # actually looks. A file that will not import fails every test in it, so
    # the pass count drops by dozens and the cause is one line, thousands of
    # lines up: thirteen red runs read as "70 tests broke" when the truth was
    # one SyntaxError in one module that 26 files import.
    if unimportable:
        print(f"\n{len(unimportable)} file(s) could not be imported at all. "
              f"That is usually one broken module, not many broken tests:")
        for name in unimportable:
            print(f"  {name}")
    # Skips are named, not buried. A test that could not run is not a test
    # that passed, and a suite quietly skipping half of itself is the failure
    # this file exists to prevent.
    if skipped:
        print(f"\n{len(skipped)} skipped:")
        for name, why in skipped:
            print(f"  {name}: {why}")
    print(f"\n{total - bad - len(skipped)}/{total} passed across "
          f"{len(files)} files, {len(skipped)} skipped, {bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
