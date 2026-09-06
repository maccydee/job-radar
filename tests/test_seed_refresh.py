"""The gate on the unattended seed rebuild.

This runs on a schedule and writes to a public release, so the failure to
design against is not "the build crashes", which is loud and stops there. It
is "the build half works and publishes anyway". A short seed is not visibly
broken: it is a seed with fewer jobs in it, and the jobs that fell off look
exactly like jobs that do not exist. Nobody downloading it would ever know.

So the checks are about the shapes a partial fetch takes, not about whether
the command exited zero.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import refresh_seed as rs  # noqa: E402


def _idx(**shards):
    return {"schema": 1, "shards": {n: {"roles": r, "bytes": r * 900}
                                    for n, r in shards.items()}}


GOOD = _idx(US=151_000, UK=19_700, unplaced=16_200, multiple=5_000,
            CA=9_100, IN=7_500, DE=6_200)


def test_a_build_that_lost_most_of_its_roles_is_refused():
    small = _idx(US=40_000, UK=5_000, unplaced=4_000, multiple=1_000)
    assert rs.check(small, GOOD), "a build a third the size was published"


def test_a_build_that_shrank_evenly_is_refused_by_the_fraction_alone():
    """The case above drops CA, IN and DE entirely, so it is caught by the
    absent-shard rule and never reaches the percentage one. Deleting
    `fresh < prev * MIN_FRACTION` outright left the whole suite green, which
    means the guard that actually stops a collapsed build being published was
    the one thing here nothing tested.

    This build keeps every shard and simply halves each of them: no shard is
    absent, no shard is empty, and the only thing wrong with it is the total.
    That is the shape of a fetch that was throttled everywhere rather than
    broken in one place, and it is the one a role count is the only witness
    to.
    """
    half = _idx(**{k: v["roles"] // 2 for k, v in GOOD["shards"].items()})
    problems = rs.check(half, GOOD)
    assert any("%" in p for p in problems), problems
    assert not any("absent" in p for p in problems), (
        "this build is meant to reach the percentage check with every shard "
        f"present, and it did not: {problems}")


def test_a_first_run_with_nothing_published_still_has_a_floor():
    """There is nothing to compare against, and "no previous build" must not
    mean "anything goes"."""
    assert rs.check(_idx(US=900, unplaced=10, multiple=5), None)
    assert rs.check(GOOD, None) == []


def test_losing_a_whole_platform_is_caught_even_though_the_count_survives():
    """The one a role count alone cannot see.

    Workable is about 7% of the roles and 60% of the runtime, so a build that
    fetched none of it is still 93% of a good one and sails past any
    percentage check. What gives it away is that shards which had roles last
    time have none now.
    """
    lost = _idx(US=151_000, UK=19_700, unplaced=16_200, multiple=5_000,
                CA=9_100, IN=7_500)          # DE gone entirely
    problems = rs.check(lost, GOOD)
    assert any("absent" in p for p in problems), problems


def test_a_missing_everybody_shard_is_refused():
    """`unplaced` and `multiple` go to every reader, so losing either hides
    those roles from everyone at once rather than from one country."""
    for drop in ("unplaced", "multiple"):
        shards = {k: v["roles"] for k, v in GOOD["shards"].items() if k != drop}
        problems = rs.check(_idx(**shards), GOOD)
        assert any(drop in p for p in problems), (drop, problems)


def test_a_normal_weeks_movement_is_not_a_failure():
    """The threshold has to be wider than the market and narrower than a
    broken fetch, or the guard becomes something people switch off."""
    for factor in (0.92, 0.97, 1.0, 1.15):
        moved = _idx(**{k: int(v["roles"] * factor)
                        for k, v in GOOD["shards"].items()})
        assert rs.check(moved, GOOD) == [], factor


def test_the_checks_run_before_anything_is_uploaded():
    """The order is the whole guard: a check that runs after the upload has
    already published whatever it was going to refuse.

    Written as `src.index("problems = check(") < src.index("gh")`, which found
    "gh" at offset 2512 -- inside the word "through", in a comment -- while the
    two real `gh release upload` calls sit at 3042 and 3392. So it compared the
    check against a piece of prose and would have stayed green with the upload
    moved above it. Reword that comment and the assertion silently changes
    meaning. Parsed here instead, off the call itself.
    """
    import ast
    import inspect
    import textwrap

    src = inspect.getsource(rs.main)
    fn = ast.parse(textwrap.dedent(src)).body[0]

    checks = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Name) and n.func.id == "check"]
    # Matched on `release upload` inside the argument list rather than on a
    # literal "gh" in the first slot. The binary is now resolved at runtime by
    # `find_gh`, because launchd's PATH does not have it and the upload was
    # dying with FileNotFoundError after a full hour of building, so the first
    # element is a Name and not a Constant. Keying on the executable's spelling
    # made this guard silently watch nothing the moment that changed.
    def _uploads(call):
        for a in call.args:
            if not isinstance(a, ast.List):
                continue
            words = [e.value for e in a.elts if isinstance(e, ast.Constant)]
            if "release" in words and "upload" in words:
                return True
        return False

    uploads = [n.lineno for n in ast.walk(fn)
               if isinstance(n, ast.Call) and _uploads(n)]
    assert checks, "nothing in main() calls check() any more"
    assert uploads, "no `gh` upload found; this guard is watching nothing"
    assert min(checks) < min(uploads), (
        f"check() runs at line {min(checks)} and the first gh upload at "
        f"{min(uploads)}: the build is published before it is checked")
    assert "staging" in src, "a failed build overwrites the good one in place"
