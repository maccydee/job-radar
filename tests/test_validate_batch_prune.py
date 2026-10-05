"""A backlog bigger than the weekly cap has to drain, a batch at a time.

On 4 October 2026 the weekly validation finished normally and then refused to
open its pull request: 308 boards were due for removal, above the workflow's
cap of 250. Nothing was deleted, which was the guard working. But the 308 were
not a platform outage. `sources/empty-since.json` was first committed on 12
September, a board has to be empty for 21 days before it may go, and 341 of
its rows dated from that first run or the one the next morning. The whole
cohort came of age on the same Sunday.

Under a cap that refuses outright, a cohort that size refuses every week for
ever: nothing is removed, so the next Sunday finds the same boards still due,
plus that week's. A guard that can never pass is a broken prune that reports
itself as a careful one.

So the cap now limits a run instead of vetoing it. `validate --prune
--max-prune N` removes at most N, oldest-empty first, and leaves the rest in
the list with their emptiness history intact for the next run. The checks
that mean "something upstream is broken" still refuse the whole pull request:
a row queued for deletion that is not safe to delete, a report that does not
say what it removed, and a diff that disagrees with its title.

The second half of this file executes the workflow's own Python, extracted
from validate.yml, against reports built here, because a step that greps
correctly and computes wrongly is the shape this repo keeps shipping.
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jobradar import cli  # noqa: E402
from test_loose_ends import _Tmp, _row, _source_file, _validate_args  # noqa: E402
from test_workflows import _load, _step_named  # noqa: E402

PLATFORM = "greenhouse"


def _day(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def _quiet_url(i: int) -> str:
    return f"https://quiet.example.test/{i:02d}"


def _seed_log(state: Path, ages: dict) -> None:
    """An emptiness log in which every quiet board is already settled: empty
    on several earlier runs, the first of them `age` days ago."""
    state.mkdir(parents=True, exist_ok=True)
    rows = {url: {"first_empty": _day(age), "last_empty": _day(7), "runs": 4}
            for url, age in ages.items()}
    (state / "empty-since.json").write_text(json.dumps(
        {"version": 1, "min_runs": 3, "min_days": 21, "sources": rows}),
        encoding="utf-8")


def _run(ages: dict, live: int = 60, reverse: bool = False, **over):
    """Validate a list of quiet boards (with the given emptiness ages) and
    live ones, every board stubbed, nothing on the network."""
    real = cli.validate_source
    quiet = [{"company": f"Quiet {u[-2:]}", "url": u, "platform": PLATFORM}
             for u in ages]
    if reverse:
        quiet.reverse()
    entries = quiet + [{"company": f"Live {i}",
                        "url": f"https://live.example.test/{i}",
                        "platform": PLATFORM} for i in range(live)]
    try:
        with _Tmp() as tmp:
            f = _source_file(tmp, entries)
            _seed_log(tmp / "state", ages)

            def fake(src):
                if "quiet" in src.url:
                    return _row(src.company, src.url, "dead", True)
                return _row(src.company, src.url, "live", False)
            cli.validate_source = fake

            rc = cli.cmd_validate(_validate_args(tmp, f, prune=True, **over))
            report = json.loads((tmp / "report.json").read_text(encoding="utf-8"))
            left = json.loads(f.read_text(encoding="utf-8"))
            log = json.loads((tmp / "state" / "empty-since.json")
                             .read_text(encoding="utf-8"))
    finally:
        cli.validate_source = real
    return rc, report, left, log


# Eight settled boards, the oldest first: 60, 59, ... 53 days empty.
AGES = {_quiet_url(i): 60 - i for i in range(8)}


# ------------------------------------------------------------ the CLI batch


def test_over_the_cap_prunes_exactly_the_cap_oldest_empty_first():
    rc, report, left, _ = _run(AGES, max_prune=3)
    assert rc == 0
    removed = [r["url"] for r in report["pruned"]]
    assert removed == [_quiet_url(0), _quiet_url(1), _quiet_url(2)], \
        f"expected the three longest-empty boards, got {removed}"
    deferred = [r["url"] for r in report["deferred"]]
    assert deferred == [_quiet_url(i) for i in range(3, 8)]
    kept = {s["url"] for s in left["sources"]}
    assert not kept & set(removed)
    assert set(deferred) <= kept, "a deferred board was removed anyway"
    assert len(left["sources"]) == 8 + 60 - 3
    assert left["meta"]["pruned"] == 3


def test_deferred_boards_keep_their_emptiness_history():
    """Deferral must not reset the clock. A deferred board that lost its
    history would need another three weeks to come due, and the backlog would
    never drain, just slowly instead of not at all."""
    _, report, _, log = _run(AGES, max_prune=3)
    rows = log["sources"]
    for r in report["deferred"]:
        url = r["url"]
        assert url in rows, f"{url} was deferred and lost its history"
        assert rows[url]["first_empty"] == _day(AGES[url]), \
            "deferral moved the date the board was first seen empty"
        assert rows[url]["runs"] == 5, "today's empty answer was not recorded"
        assert rows[url]["last_empty"] == date.today().isoformat()
        assert r["empty_since"] == _day(AGES[url])


def test_the_batch_is_the_same_whatever_order_the_list_is_in():
    """Same first-empty date, the URL breaks the tie, so two runs over the
    same evidence offer the same pull request."""
    same = {_quiet_url(i): 40 for i in range(6)}
    _, a, _, _ = _run(same, max_prune=2)
    _, b, _, _ = _run(same, max_prune=2, reverse=True)
    assert [r["url"] for r in a["pruned"]] == [_quiet_url(0), _quiet_url(1)]
    assert [r["url"] for r in b["pruned"]] == [r["url"] for r in a["pruned"]]


def test_under_the_cap_nothing_is_deferred():
    _, report, left, _ = _run(AGES, max_prune=250)
    assert len(report["pruned"]) == 8
    assert report["deferred"] == []
    assert len(left["sources"]) == 60


def test_without_the_option_every_settled_board_goes_and_deferred_is_empty():
    """A local `validate --prune` is unchanged, and still says what it held
    back, so the workflow can tell 'nothing deferred' from 'did not say'."""
    _, report, left, _ = _run(AGES)
    assert len(report["pruned"]) == 8
    assert report["deferred"] == []
    assert len(left["sources"]) == 60


def test_the_quarter_refusal_still_refuses_with_a_batch_limit():
    """Guard 3 reasons about everything that came back empty, not about the
    batch. A limit on how many go must not become a way past it."""
    rc, report, left, _ = _run(AGES, live=10, max_prune=3)
    assert rc == 1
    assert len(left["sources"]) == 18, "a refused prune changed the list"
    assert "pruned" not in report


def test_max_prune_takes_a_positive_count_and_refuses_anything_else():
    """Zero would defer every board and prune none, every week, while the
    run reported a clean prune. A negative is a typo. Both are refused at
    the command line rather than meaning something."""
    import contextlib
    import io
    parser = cli.build_parser()
    args = parser.parse_args(["validate", "--max-prune", "250"])
    assert args.max_prune == 250
    assert parser.parse_args(["validate"]).max_prune is None, \
        "no option means no limit, as before"
    for bad in ("0", "-1", "x"):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                parser.parse_args(["validate", "--max-prune", bad])
            except SystemExit as e:
                assert e.code == 2, (bad, e.code)
            else:
                raise AssertionError(f"--max-prune {bad} was accepted")
        assert "--max-prune" in err.getvalue(), err.getvalue()


# ------------------------------------------- the workflow, executed for real


def _heredoc(run: str, tag: str) -> str:
    """The Python between `<<'TAG'` and the closing `TAG` line."""
    lines = run.splitlines()
    start = next(i for i, l in enumerate(lines) if f"<<'{tag}'" in l)
    end = next(i for i in range(start + 1, len(lines))
               if lines[i].strip() == tag)
    return "\n".join(lines[start + 1:end]) + "\n"


def _cap() -> int:
    return int(_load("validate.yml")["jobs"]["validate"]["env"]["PRUNE_CAP"])


def _summarise(report: dict, cap: int | None = None) -> tuple[dict, str]:
    """Run the Summarise step's own Python against `report`; return its
    GITHUB_OUTPUT as a dict, and the summary it wrote. `cap` stands in for
    the job's PRUNE_CAP, so a small fixture can fill a batch."""
    wf = _load("validate.yml")
    code = _heredoc(_step_named(wf, "validate", "Summarise")["run"], "PY")
    with _Tmp() as tmp:
        (tmp / "out").mkdir()
        (tmp / "out" / "validation.json").write_text(json.dumps(report),
                                                    encoding="utf-8")
        env = dict(os.environ,
                   GITHUB_STEP_SUMMARY=str(tmp / "summary.md"),
                   GITHUB_OUTPUT=str(tmp / "output.txt"),
                   PRUNE_CAP=str(cap if cap is not None
                                 else wf["jobs"]["validate"]["env"]["PRUNE_CAP"]))
        p = subprocess.run([sys.executable, "-c", code], cwd=tmp, env=env,
                           capture_output=True, text=True, encoding="utf-8")
        assert p.returncode == 0, p.stderr
        out = {}
        for line in (tmp / "output.txt").read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            out[k] = v
        summary = (tmp / "summary.md").read_text(encoding="utf-8")
    return out, summary


def _dead(i: int, **over) -> dict:
    row = {"company": f"Gone {i}", "url": f"https://gone.example.test/{i}",
           "platform": PLATFORM, "live_jobs": 0, "verdict": "dead",
           "transport": None, "prunable": True, "note": ""}
    row.update(over)
    return row


def _report(pruned, deferred, total=17000):
    rows = list(pruned) + list(deferred)
    return {"total": total, "dead": rows, "mismatch": [], "rows": rows,
            "pruned": pruned, "deferred": deferred, "waiting": 0}


def test_a_full_batch_with_a_backlog_opens_a_pull_request_that_says_so():
    cap = _cap()
    pruned = [_dead(i) for i in range(cap)]
    deferred = [_dead(cap + i) for i in range(58)]
    out, summary = _summarise(_report(pruned, deferred))
    assert out["refuse"] == "false", summary
    assert out["dead"] == str(cap)
    assert out["deferred"] == "58"
    assert out["title"] == (f"Prune {cap} of {cap + 58} dead source(s); "
                            f"58 deferred to the next run")
    assert "58" in out["batch_note"] and "next" in out["batch_note"]
    assert "deferred to the next run" in summary and "**58**" in summary


def test_an_ordinary_week_is_titled_as_before():
    out, _ = _summarise(_report([_dead(1), _dead(2)], []))
    assert out["refuse"] == "false"
    assert out["title"] == "Prune 2 dead source(s)"
    assert out["deferred"] == "0"
    assert out["batch_note"] == ""


def test_an_unsafe_row_refuses_the_whole_batch_even_over_the_cap():
    cap = _cap()
    for where in ("pruned", "deferred"):
        pruned = [_dead(i) for i in range(cap)]
        deferred = [_dead(cap + i) for i in range(58)]
        bad = _dead(9999, transport="UNEXPECTED_EOF_WHILE_READING",
                    prunable=False, verdict="unreachable")
        (pruned if where == "pruned" else deferred)[5] = bad
        out, summary = _summarise(_report(pruned, deferred))
        assert out["refuse"] == "true", f"unsafe row in {where} was let through"
        assert "Gone 9999" in summary


def test_a_report_without_pruned_still_refuses():
    rep = _report([_dead(1)], [])
    del rep["pruned"]
    out, _ = _summarise(rep)
    assert out["refuse"] == "true"


def test_a_report_without_deferred_refuses():
    """'Did not say what it held back' is not 'held nothing back'. The title
    would claim no deferral on a run that never measured one."""
    rep = _report([_dead(1)], [])
    del rep["deferred"]
    out, _ = _summarise(rep)
    assert out["refuse"] == "true"


def test_more_than_the_cap_in_one_batch_still_refuses():
    """validate was told to stop at the cap. Past it, the limit did not
    hold, and that is a fault upstream rather than a backlog."""
    cap = _cap()
    out, _ = _summarise(_report([_dead(i) for i in range(cap + 1)], []))
    assert out["refuse"] == "true"


def test_a_deferral_from_a_batch_that_is_not_full_refuses():
    """Deferral only happens when the batch hits the cap. Anything deferred
    beside a short batch means the two disagree about what the cap is."""
    out, _ = _summarise(_report([_dead(1)], [_dead(2)]))
    assert out["refuse"] == "true"


def test_the_refusal_step_does_not_claim_the_cap_refused():
    wf = _load("validate.yml")
    step = _step_named(wf, "validate", "Refuse the prune")
    assert "REASON" in step.get("env", {}), \
        "the refusal should say why, from the summary, not from a stock line"


def test_the_failure_issue_no_longer_says_the_cap_refuses():
    run = _step_named(_load("validate.yml"), "validate", "Report a failed run")["run"]
    assert "more than 250 boards were due for deletion" not in run
    assert "batch" in run


# ----------------------------------------- the claim against the diff, run


def _diff_check(base: list, head: list, claimed: int) -> subprocess.CompletedProcess:
    wf = _load("validate.yml")
    code = _heredoc(_step_named(wf, "validate", "The diff has to match the claim")
                    ["run"], "EOF")
    with _Tmp() as tmp:
        (tmp / "sources").mkdir()
        (tmp / "base.json").write_text(json.dumps({"sources": base}),
                                       encoding="utf-8")
        (tmp / "sources" / "sources.json").write_text(
            json.dumps({"sources": head}), encoding="utf-8")
        env = dict(os.environ, DEAD=str(claimed), RUNNER_TEMP=str(tmp))
        return subprocess.run([sys.executable, "-c", code], cwd=tmp, env=env,
                              capture_output=True, text=True, encoding="utf-8")


def test_the_diff_check_passes_a_matching_batch_and_fails_a_mismatch():
    base = [{"company": f"C{i}", "url": f"https://x.example.test/{i}"}
            for i in range(20)]
    head = base[5:]
    assert _diff_check(base, head, 5).returncode == 0
    bad = _diff_check(base, head, 4)
    assert bad.returncode == 1 and "does not match the claim" in bad.stdout
    # A batch claim measured against the whole backlog is the mismatch this
    # change could most easily introduce: titled 8, diff removes 5.
    assert _diff_check(base, head, 8).returncode == 1


def test_end_to_end_a_batched_prune_matches_its_own_claim():
    """CLI batch, then the workflow's summary, then the workflow's diff check,
    all against the same files. The number in the title is the number the
    diff removes."""
    real = cli.validate_source
    ages = {_quiet_url(i): 60 - i for i in range(8)}
    entries = ([{"company": f"Quiet {i}", "url": _quiet_url(i),
                 "platform": PLATFORM} for i in range(8)]
               + [{"company": f"Live {i}", "url": f"https://live.example.test/{i}",
                   "platform": PLATFORM} for i in range(60)])
    try:
        with _Tmp() as tmp:
            f = _source_file(tmp, entries)
            _seed_log(tmp / "state", ages)
            cli.validate_source = lambda src: _row(
                src.company, src.url, "dead" if "quiet" in src.url else "live",
                "quiet" in src.url)
            assert cli.cmd_validate(_validate_args(tmp, f, prune=True,
                                                   max_prune=3)) == 0
            report = json.loads((tmp / "report.json").read_text(encoding="utf-8"))
            head = json.loads(f.read_text(encoding="utf-8"))["sources"]
    finally:
        cli.validate_source = real
    # The same number the CLI was given, as the workflow passes PRUNE_CAP to
    # both. Three removed, five deferred, and the title says exactly that.
    out, _ = _summarise(report, cap=3)
    assert out["refuse"] == "false", out
    assert out["dead"] == "3" and out["deferred"] == "5"
    assert out["title"] == "Prune 3 of 8 dead source(s); 5 deferred to the next run"
    assert _diff_check(entries, head, int(out["dead"])).returncode == 0
    # Claiming the backlog instead of the batch fails, and so does a summary
    # given a different cap from the one validate used.
    assert _diff_check(entries, head, 8).returncode == 1
    out, _ = _summarise(report, cap=4)
    assert out["refuse"] == "true", "a 3-board batch beside deferrals under a cap of 4"


# ------------------------------------------------------------ the artefact


def test_the_report_is_uploaded_whatever_happened():
    wf = _load("validate.yml")
    steps = wf["jobs"]["validate"]["steps"]
    names = [s.get("name", "") for s in steps]
    hits = [i for i, s in enumerate(steps)
            if str(s.get("uses", "")).startswith("actions/upload-artifact@")]
    assert len(hits) == 1, "the validation report is never uploaded"
    i = hits[0]
    step = steps[i]
    assert step["uses"] == "actions/upload-artifact@v4"
    assert step["if"] == "always()", "a refused run is the one that needs it"
    assert i > names.index("Check every source")
    w = step["with"]
    assert "out/validation.json" in str(w["path"])
    assert w["if-no-files-found"] == "warn"
    assert 1 <= int(w["retention-days"]) <= 90


def test_the_workflow_passes_its_cap_to_validate():
    wf = _load("validate.yml")
    run = _step_named(wf, "validate", "Check every source")["run"]
    assert '--max-prune "$PRUNE_CAP"' in run
    assert "--force-prune" not in run
    assert 'os.environ["PRUNE_CAP"]' in _step_named(wf, "validate", "Summarise")["run"]
