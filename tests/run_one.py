"""Run one test file with the same collector `run_all` uses.

`run_all` runs everything, which takes minutes. A red-green cycle wants one
file in seconds. This reuses its loader so a test that passes here passes there.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import run_all  # noqa: E402


def main(argv) -> int:
    if len(argv) < 2:
        print("usage: python3 tests/run_one.py tests/test_x.py [name-substring]")
        return 2
    path = Path(argv[1])
    if not path.exists():
        print(f"no such test file: {path}")
        return 2
    only = argv[2] if len(argv) > 2 else ""
    mod = run_all._load(path.resolve())
    bad = total = 0
    for name, fn in run_all._collect(mod):
        if only and only not in name:
            continue
        total += 1
        verdict, why = run_all.run_one(fn)
        print(f"  {verdict:4}  {name}" + (f"  {why}" if why else ""))
        if verdict == "fail":
            bad += 1
            # Re-run to show the traceback; run_one swallows it.
            try:
                fn()
            except BaseException:
                import traceback
                traceback.print_exc()
    print(f"{total - bad}/{total} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
