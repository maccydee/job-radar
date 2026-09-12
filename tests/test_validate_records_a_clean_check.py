"""A validation that deletes nothing still has to say it ran.

On 12 September 2026 the dashboard said the source list was last checked 18
days earlier. The weekly job had been committing only when it pruned
something, so any run that did not prune left `meta.checked` where it was,
upstream and in every clone.

That had been survivable while one Sunday of emptiness was enough to delete a
board. It stopped being survivable when a board had to stay empty for weeks
(jobradar/deadwood.py), for two reasons that are the same bug:

  * The job still read the report's `dead`, every board empty TODAY, as the
    deletion list. 348 came back empty on 7 September, over its cap of 250,
    so it refused, while the number the prune would actually remove was zero.
  * Refused or clean, nothing was committed, so `sources/empty-since.json`
    was thrown away with the runner. No board could ever reach the thresholds,
    so the prune the log exists to allow would never happen.

A guard that can never pass, and a freshness date that can never move. Both
rendered as a quiet week.
"""

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jobradar import cli  # noqa: E402
from test_loose_ends import _Tmp, _row, _source_file, _validate_args  # noqa: E402
from test_workflows import _code, _load, _step_named  # noqa: E402


def _run_all_empty(n=6):
    """A first validation where every board answers empty and none has any
    history, which is what the first Sundays under the waiting rule look like."""
    real = cli.validate_source
    try:
        with _Tmp() as tmp:
            f = _source_file(tmp, [{"company": f"Quiet {i}",
                                    "url": f"https://quiet.example.test/{i}"}
                                   for i in range(n)])
            # Kept under the tool's own quarter-of-the-list refusal by adding
            # live boards, so this exercises the waiting rule and not that.
            entries = json.loads(f.read_text(encoding="utf-8"))["sources"]
            entries += [{"company": f"Live {i}",
                         "url": f"https://live.example.test/{i}"}
                        for i in range(n * 4)]
            f.write_text(json.dumps({"sources": entries}), encoding="utf-8")

            def fake(src):
                if "quiet" in src.url:
                    return _row(src.company, src.url, "dead", True)
                return _row(src.company, src.url, "live", False)
            cli.validate_source = fake

            rc = cli.cmd_validate(_validate_args(tmp, f, prune=True))
            report = json.loads((tmp / "report.json").read_text(encoding="utf-8"))
            left = json.loads(f.read_text(encoding="utf-8"))
            log_written = (tmp / "state" / "empty-since.json").exists()
    finally:
        cli.validate_source = real
    return rc, report, left, log_written


def test_the_report_separates_empty_today_from_about_to_be_deleted():
    rc, report, left, _ = _run_all_empty()
    assert rc == 0
    assert len(report["dead"]) == 6, "every quiet board is still reported as empty"
    assert "pruned" in report, \
        "the report has to say what the prune removes, or the job guesses from `dead`"
    assert report["pruned"] == [], "nothing has been empty for weeks yet"
    assert report["waiting"] == 6
    # And the two files the clean-week commit carries are both there to carry.
    # The CLI already did this before the fix; it is asserted here, beside the
    # part that went red, because the new workflow step is worthless without it.
    _, _, left, log_written = _run_all_empty()
    assert len(left["sources"]) == 30, "a first empty Sunday deletes nothing"
    assert left["meta"]["checked"] == date.today().isoformat()
    assert log_written


def test_the_weekly_job_counts_deletions_not_empty_boards():
    run = _step_named(_load("validate.yml"), "validate", "Summarise")["run"]
    assert 'r.get("pruned")' in run, \
        "the cap and the pull request title have to count what is removed"
    # A report with no `pruned` field cannot say what it deletes, which is
    # not the same as saying it deletes nothing.
    assert "no_claim" in run and "refuse = no_claim" in run


def test_a_clean_week_is_committed():
    wf = _load("validate.yml")
    step = _step_named(wf, "validate", "Record a clean check")
    assert step["if"] == ("steps.sum.outputs.dead == '0' && "
                          "steps.sum.outputs.refuse != 'true'")
    code = _code(step["run"])
    assert "sources/sources.json" in code and "sources/empty-since.json" in code
    assert "git push" in code
    # The retry must not report success after three failed pushes.
    assert 'pushed=yes' in code and '[ -z "$pushed" ]' in code and "exit 1" in code


def test_the_diff_check_runs_before_a_clean_commit_too():
    wf = _load("validate.yml")
    steps = wf["jobs"]["validate"]["steps"]
    names = [s.get("name", "") for s in steps]
    guard = names.index("The diff has to match the claim")
    clean = names.index("Record a clean check")
    assert guard < clean
    assert steps[guard]["if"] == "steps.sum.outputs.refuse != 'true'", \
        "a claim of zero removals is still a claim, and has to be checked"
