"""What the unattended workflows must keep doing.

These four YAML files are the only part of the project that runs with nobody
watching. Everything else fails in front of a person; a workflow fails into a
tab, on a Sunday, and looks exactly like a quiet week. So the properties that
make the difference between "it broke" and "it broke and stayed broken" are
asserted here rather than eyeballed at review.

Three of them are about a specific thing that has already gone wrong:

  * `config.yaml` stopped being tracked on 2026-08-25 and is gitignored, so a
    checkout on a fresh fork has no config in it and `job-radar scan` stops on
    "No config at config.yaml". The scan workflow has to say that in words a
    fork owner can act on.
  * The last scheduled `validate` run checked 653 sources in 1m45s. The list
    is now 17,810 entries, and 2,094 of them are on a host paced at 0.7
    requests a second, which is a 50 minute floor on that host alone. Nothing
    about the old runtime says anything about the new one, and a job with no
    timeout is killed by GitHub at 360 minutes with no report.
  * `validate --prune` deletes bundled sources every Sunday. A throttled
    board, an unreachable board and a TLS handshake failure are none of them
    evidence that an employer is gone.

Nothing here touches the network, runs a workflow, or reads anything outside
the repository.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"

# GitHub kills a job at 360 minutes whatever the file says, so any declared
# timeout has to be under that to mean anything.
RUNNER_HARD_LIMIT = 360
# apply.workable.com holds 2,094 of the 17,810 entries at 0.7 requests a
# second. 2094 / 0.7 is 2,991 seconds before anything else is counted.
WORKABLE_FLOOR_MINUTES = 49


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _text(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def _files() -> list[Path]:
    return sorted(WORKFLOWS.glob("*.yml"))


def _steps(wf: dict, job: str) -> list[dict]:
    return wf["jobs"][job]["steps"]


def _step(wf: dict, job: str, needle: str) -> dict:
    """The one step whose name or `uses` contains `needle`."""
    hits = [s for s in _steps(wf, job)
            if needle.lower() in (s.get("name") or s.get("uses") or "").lower()]
    assert len(hits) == 1, f"expected one step matching {needle!r}, got {len(hits)}"
    return hits[0]


def _step_named(wf: dict, job: str, name: str) -> dict:
    """The step with exactly this name. Substring matching is not enough:
    "Scan" is also inside "Check there is a config to scan with"."""
    hits = [s for s in _steps(wf, job) if s.get("name") == name]
    assert len(hits) == 1, f"expected one step named {name!r}, got {len(hits)}"
    return hits[0]


def _code(run: str) -> str:
    """A run block with its comment lines dropped. Half of what these steps
    say is an explanation of the bug they exist to stop, and a test looking
    for `sed -i` would otherwise find it in the note saying sed was removed."""
    return "\n".join(l for l in run.splitlines() if not l.lstrip().startswith("#"))


def _runs(wf: dict) -> list[str]:
    out = []
    for job in wf["jobs"].values():
        for s in job.get("steps", []):
            if isinstance(s.get("run"), str):
                out.append(s["run"])
    return out


# ------------------------------------------------------------------ parsing


def test_there_are_four_workflows_and_they_all_parse():
    names = {p.name for p in _files()}
    assert names == {"scan.yml", "sync-skills.yml", "test.yml", "validate.yml"}
    for p in _files():
        assert isinstance(yaml.safe_load(p.read_text(encoding="utf-8")), dict), p.name


def test_every_workflow_still_has_a_trigger():
    # `on` is YAML 1.1's boolean true, so safe_load gives back the key True.
    # A test that looked for the string "on" would pass on a file with no
    # trigger at all, which is a workflow that never runs and never says so.
    for p in _files():
        wf = yaml.safe_load(p.read_text(encoding="utf-8"))
        trigger = wf.get(True, wf.get("on"))
        assert trigger, f"{p.name} has no trigger"


# ------------------------------------------------------------------ em-dashes


def test_no_em_dashes_anywhere_in_the_workflows():
    """Two were found in issue bodies earlier today. The bodies are prose that
    gets posted under the owner's name, so they are held to the same rule as
    anything else written for him."""
    # Written as escapes so that this file does not itself contain the
    # characters it is here to keep out.
    dashes = {"\u2014": "em-dash", "\u2013": "en-dash"}
    for p in _files():
        text = p.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            for ch, what in dashes.items():
                assert ch not in line, f"{what} in {p.name} line {i}: {line.strip()}"


# ------------------------------------------------------------------ timeouts


def test_every_job_declares_a_timeout_below_the_runner_hard_limit():
    for p in _files():
        wf = yaml.safe_load(p.read_text(encoding="utf-8"))
        for name, job in wf["jobs"].items():
            t = job.get("timeout-minutes")
            assert t is not None, f"{p.name}: job {name} has no timeout-minutes"
            assert 0 < t < RUNNER_HARD_LIMIT, (
                f"{p.name}: job {name} timeout {t} is not below the "
                f"{RUNNER_HARD_LIMIT} minute limit GitHub enforces anyway")


def test_the_long_running_steps_have_room_for_the_reporting_steps_after_them():
    """A step timeout below the job timeout is the whole point: the job keeps
    enough life to commit what it has and to say that it failed. A job
    cancelled at its own limit runs nothing further, including the step that
    files the issue."""
    for fname, job, step in [("scan.yml", "scan", "Scan"),
                             ("validate.yml", "validate", "Check every source")]:
        wf = _load(fname)
        s = _step_named(wf, job, step)
        step_t = s.get("timeout-minutes")
        job_t = wf["jobs"][job]["timeout-minutes"]
        assert step_t is not None, f"{fname}: {step} has no timeout"
        assert step_t < job_t, (
            f"{fname}: {step} may use the whole job budget, leaving nothing "
            f"for the steps that report the failure")
        assert step_t > WORKABLE_FLOOR_MINUTES, (
            f"{fname}: {step_t} minutes is under the {WORKABLE_FLOOR_MINUTES} "
            f"minute floor that apply.workable.com alone imposes")


# ------------------------------------------------------------------ the fork


def test_the_scan_checks_for_a_config_before_it_scans():
    """config.yaml is gitignored, so a fresh fork checks out without one and
    the scan dies inside the tool on advice a runner cannot follow ("run
    job-radar setup"). The fix a fork owner needs is `git add -f`, and this is
    the only place it can be said early enough to be useful."""
    wf = _load("scan.yml")
    names = [s.get("name", "") for s in _steps(wf, "scan")]
    check = _step_named(wf, "scan", "Check there is a config to scan with")
    assert names.index(check["name"]) < names.index("Scan"), \
        "the config check has to come before the scan, not after it"
    run = check["run"]
    assert "config.yaml" in run and "config.local.yaml" in run, \
        "both config names are searched by the loader, so both have to be accepted"
    assert "add -f" in run, "the message has to name the fix, which is `git add -f`"
    assert "::error::" in run, "an annotation, so it shows without opening the log"
    assert "exit 1" in run, "a missing config has to stop the run"


def test_the_scan_force_adds_the_state_file_and_nothing_else():
    """`state/` is gitignored and a fork has to commit it or nothing is ever
    new. Force-adding the directory would also force-add whatever else has
    been dropped in there; the tool only ever writes state/seen.json."""
    run = _code(_step_named(_load("scan.yml"), "scan", "Commit what we have seen")["run"])
    assert "git add -f state/seen.json" in run
    assert "git add -f state/\n" not in run and "git add -f state/ " not in run
    assert "git add -A" not in run and "git add ." not in run, \
        "a blanket add would sweep in a personal config or a database"
    for forbidden in ("config.yaml", "config.local.yaml", "data/", "out/",
                      ".backups/", "applications.local.yaml"):
        assert f"add -f {forbidden}" not in run, \
            f"{forbidden} must never be force-added past the ignore rule"


def test_the_scan_will_not_commit_a_half_written_state_file():
    """State is written with a plain write_text, so a scan killed at the
    timeout can leave truncated JSON. The loader treats unparseable JSON as an
    empty state, so committing it makes every role look new again and then
    overwrites the file as though that were correct."""
    run = _code(_step_named(_load("scan.yml"), "scan", "Commit what we have seen")["run"])
    assert "json.load" in run, "the file has to be parsed before it is committed"
    assert "encoding='utf-8'" in run or 'encoding="utf-8"' in run
    assert run.index("json.load") < run.index("git add -f state/seen.json"), \
        "the parse check has to run before the add, not after it"


def test_a_push_that_never_succeeds_fails_the_step():
    """The retry loop used to end on the third rejection with the exit status
    of `sleep`, so three rejected pushes reported a green tick over a run
    whose state never left the runner."""
    run = _code(_step_named(_load("scan.yml"), "scan", "Commit what we have seen")["run"])
    assert "pushed=" in run, "the loop has to record whether any attempt worked"
    assert 'if [ -z "$pushed" ]' in run
    tail = run[run.index('if [ -z "$pushed" ]'):]
    assert "::error::" in tail and "exit 1" in tail


# ------------------------------------------------------------------ noticing


def test_every_unattended_workflow_reports_its_own_failure():
    """Scheduled runs are the ones nobody watches. A red tick in the Actions
    tab is not a report; an issue is."""
    for fname, job in [("scan.yml", "scan"), ("validate.yml", "validate"),
                       ("sync-skills.yml", "sync")]:
        wf = _load(fname)
        report = _step_named(wf, job, "Report a failed run")
        assert report["if"] == "failure()", fname
        assert report["continue-on-error"] is True, (
            f"{fname}: a repo with Issues disabled must not have one failure "
            f"turned into two")
        assert "gh issue create" in report["run"], fname
        assert "|| echo" in report["run"], (
            f"{fname}: the reporting step must not be able to fail")
        assert "actions/runs/" in str(report.get("env", {})), (
            f"{fname}: the issue has to carry a link to the run")


def test_the_workflow_that_files_an_issue_is_allowed_to():
    for fname, job in [("scan.yml", "scan"), ("validate.yml", "validate"),
                       ("sync-skills.yml", "sync")]:
        wf = _load(fname)
        perms = wf["jobs"][job].get("permissions") or wf.get("permissions") or {}
        assert perms.get("issues") == "write", (
            f"{fname} files an issue on failure and needs issues: write, or "
            f"the report itself is the thing that fails")


def test_the_dashboard_survives_a_run_that_broke():
    """The artifact upload used to sit behind an implicit success(), so the
    one run whose partial output somebody wants is the one that discards it.
    It also sat after `gh issue create`, which fails outright on a fork with
    Issues turned off."""
    wf = _load("scan.yml")
    up = _step(wf, "scan", "upload-artifact")
    assert up["if"] == "always()"
    issue = _step_named(wf, "scan", "Post new roles as an issue")
    assert "if ! gh issue create" in issue["run"], \
        "a failed issue has to fall through to the step summary, not end the job"
    assert "GITHUB_STEP_SUMMARY" in issue["run"]
    assert "out/roles.json" in issue["run"] and "-f out/roles.json" in issue["run"], \
        "an html-only config writes no roles.json and must not produce a traceback"


# ------------------------------------------------------------------ the prune


def test_the_weekly_prune_never_forces_past_its_own_refusal():
    """`--force-prune` overrides the "more than a quarter of the list came
    back empty" check, which is the only thing between a rate-limited runner
    and a pull request deleting thousands of live employers."""
    run = _code(_step_named(_load("validate.yml"), "validate", "Check every source")["run"])
    assert "--prune" in run
    assert "--force-prune" not in run
    assert "--file sources/sources.json" in run, \
        "prune refuses without --file, and would silently do nothing"


def test_the_prune_pull_request_is_blocked_when_a_row_is_not_safe_to_delete():
    """Checked at the point of deletion rather than trusted. `--prune` keys on
    the verdict; this keys on the per-row `prunable` flag and the transport
    alert, so a refactor that lets a handshake failure reach the dead branch
    is caught by different code than the code that broke."""
    wf = _load("validate.yml")
    run = _step_named(wf, "validate", "Summarise")["run"]
    assert "transport" in run, "a TLS handshake failure has to be checked for by name"
    assert "prunable" in run, "the per-row flag has to be checked, not just the verdict"
    assert 'verdict") != "dead"' in run
    assert "refuse=" in run

    pr = _step_named(wf, "validate", "Open a pull request")
    assert "steps.sum.outputs.refuse != 'true'" in pr["if"], \
        "the refusal has to gate the pull request, which is the last point " \
        "where a deletion can still be stopped"
    assert "steps.sum.outputs.dead != '0'" in pr["if"]

    refuse = _step_named(wf, "validate", "Refuse the prune")
    assert refuse["if"] == "steps.sum.outputs.refuse == 'true'"
    assert "exit 1" in refuse["run"], "a refusal is a failure, so it gets reported"


def test_the_prune_has_an_absolute_cap_as_well_as_a_share():
    """A quarter of 653 sources was 163. A quarter of 17,810 is 4,452, so the
    share-based refusal inside the tool would now wave through a pull request
    deleting four thousand employers."""
    run = _step_named(_load("validate.yml"), "validate", "Summarise")["run"]
    m = re.search(r"CAP\s*=\s*(\d+)", run)
    assert m, "there has to be an absolute cap on how many boards one week may delete"
    cap = int(m.group(1))
    assert 0 < cap <= 1000, f"a cap of {cap} out of 17,810 is not a cap"
    assert "len(dead) > CAP" in run


def test_the_summary_says_how_many_boards_could_not_be_read():
    """It reported checked, dead and mismatch only, so a week where nine
    thousand boards were unreachable looked exactly like a clean one."""
    run = _step_named(_load("validate.yml"), "validate", "Summarise")["run"]
    assert "unreachable" in run
    assert "TLS handshake failures" in run


def test_the_prune_pull_request_only_touches_the_source_list():
    """Anything wider could commit out/, a database, or a personal config.

    Asserted as a set rather than as one exact string. The prune now also
    carries `sources/empty-since.json`, the record of how long each board has
    been empty, because the runner starts from a fresh checkout and a log it
    cannot keep means no board ever reaches the thresholds and nothing is ever
    pruned. The evidence belongs in the diff with the deletion it justifies.

    What matters is the boundary, not the literal: every path is a known file
    under sources/, and nothing here reaches out/, data/ or a config.
    """
    pr = _step_named(_load("validate.yml"), "validate", "Open a pull request")
    raw = pr["with"]["add-paths"]
    paths = [x.strip() for x in str(raw).splitlines() if x.strip()]
    assert paths, "the prune pull request commits nothing"
    assert set(paths) <= {"sources/sources.json", "sources/empty-since.json"}, \
        f"unexpected path in the prune pull request: {paths}"
    for p in paths:
        assert p.startswith("sources/"), p
        assert "*" not in p and ".." not in p, p


# ------------------------------------------------------------------ secrets


def test_no_workflow_prints_a_secret_or_a_credentialled_url():
    """Reed and Adzuna keys come from the config, not from Actions secrets, so
    there is no secrets context to leak. What there is instead is an Adzuna
    URL with app_id and app_key in its query string, which the fetcher keeps
    on a throwaway probe so that nothing durable carries it. No workflow may
    reintroduce it by echoing a URL it built itself."""
    for p in _files():
        wf = yaml.safe_load(p.read_text(encoding="utf-8"))
        for run in map(_code, _runs(wf)):
            # Through _code(), like every other source grep in this file. A
            # comment saying "never echo a URL built from secrets.ADZUNA_KEY"
            # is the note explaining the rule, not a breach of it.
            assert "app_key" not in run and "app_id" not in run, p.name
            assert "secrets." not in run, (
                f"{p.name}: a secret expanded inside a run block is a secret "
                f"one `set -x` away from the log")


def test_workflows_do_not_interpolate_untrusted_expressions_into_shell():
    """`${{ github.event.* }}` and branch names are attacker-controllable on a
    pull request from a fork, and inside a `run:` block they are substituted
    before bash ever sees them. The commit step wants the branch name and
    takes it as $GITHUB_REF_NAME instead."""
    bad = re.compile(r"\$\{\{\s*github\.(event|head_ref)\b")
    for p in _files():
        wf = yaml.safe_load(p.read_text(encoding="utf-8"))
        for run in _runs(wf):
            assert not bad.search(run), f"{p.name}: untrusted expression in a run block"


def test_permissions_are_declared_everywhere_and_are_not_write_all():
    for p in _files():
        wf = yaml.safe_load(p.read_text(encoding="utf-8"))
        top = wf.get("permissions")
        assert top is not None, f"{p.name}: no top level permissions block"
        assert top != "write-all", p.name
        for name, job in wf["jobs"].items():
            effective = job.get("permissions")
            if effective is None:
                effective = top
            assert effective not in (None, "write-all"), \
                f"{p.name}: job {name} has no effective permissions"


def test_the_scan_job_does_not_hold_the_token_that_deploys_the_site():
    """Pages and the OIDC token were granted at the top of the file, so the
    step that fetches several thousand third-party job boards held a token
    that could deploy the public site. Only the publish job needs them."""
    wf = _load("scan.yml")
    scan = wf["jobs"]["scan"]["permissions"]
    assert set(scan) == {"contents", "issues"}, scan
    publish = wf["jobs"]["publish"]["permissions"]
    assert set(publish) == {"pages", "id-token"}, publish


def test_the_test_workflow_is_read_only():
    """It runs code from pull requests. Without a permissions block it
    inherits the repository default, which on an older repository is
    read/write on everything."""
    wf = _load("test.yml")
    assert wf["permissions"] == {"contents": "read"}


# ------------------------------------------------------------------ CI shape


def test_ci_runs_the_discovering_runner_so_a_new_test_file_runs():
    """Naming one file is why test_locations.py went unrun from the day it was
    added. This file would be the next one."""
    runs = " ".join(map(_code, _runs(_load("test.yml"))))
    assert "tests/run_all.py" in runs
    # Through _code(): test.yml already carries "run_all.py rather than
    # test_core.py, because naming one file meant the..." in a comment, and
    # this escaped only because that comment is above the run block rather
    # than inside it and omits the tests/ prefix.
    assert "tests/test_core.py" not in runs
    assert (ROOT / "tests" / "run_all.py").exists()


def test_the_vendored_skill_pin_is_updated_by_something_that_can_actually_work():
    """The old `sed -i -E "s|...|...|"` put a markdown table row in the
    replacement, so the third `|` ended the substitution and the rest was read
    as flags. It failed with "bad flag in substitute command" on every run and
    `|| true` swallowed it, so the copy was updated while the row recording
    which revision was copied stayed pinned."""
    run = _code(_step_named(_load("sync-skills.yml"), "sync",
                            "Copy over the vendored version")["run"])
    assert "sed -i" not in run, "the old sed could not run at all"
    assert "|| true" not in run, "a silent pass is how the pin went stale"
    assert "skills/README.md" in run
    assert 'encoding="utf-8"' in run, "the README is read and written on Windows too"


def test_config_yaml_is_still_ignored_which_is_what_all_of_this_is_about():
    """If it ever becomes tracked again, the fork instructions in scan.yml are
    wrong and the config check is dead weight. Assert the fact the workflow
    depends on rather than the workflow's opinion of it."""
    lines = [l.strip() for l in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()]
    assert "config.yaml" in lines
    assert "state/" in lines
    assert "out/" in lines


def _run() -> int:
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    bad = 0
    for name, fn in fns:
        try:
            fn()
        except AssertionError as e:
            bad += 1
            print(f"  FAIL  {name}: {e}")
        except Exception as e:  # noqa: BLE001
            bad += 1
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
        else:
            print(f"  pass  {name}")
    print(f"\n{len(fns) - bad}/{len(fns)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(_run())


def test_the_prune_pull_request_checks_its_diff_against_the_claim():
    """A pull request titled "Prune 2 dead source(s)" whose diff removed
    17,171 of 17,810 sat open for three days.

    The branch had been cut before the harvest that grew the list, so its copy
    of sources.json was a wholesale revert wearing a prune's title. Every
    guard upstream passed, because all of them reason about the run: two
    boards really were dead, two is far under the cap, and the refusal
    threshold is a share of the run's own source count. None of them look at
    the artefact that would actually be merged.
    """
    wf = _load("validate.yml")
    steps = wf["jobs"]["validate"]["steps"]
    names = [s.get("name", "") for s in steps]
    assert "The diff has to match the claim" in names

    guard = names.index("The diff has to match the claim")
    pr = next(i for i, s in enumerate(steps)
              if "create-pull-request" in str(s.get("uses", "")))
    assert guard < pr, "the check has to run before the pull request is opened"

    body = steps[guard]["run"]
    # It must compare against the base branch, not against itself. The branch
    # name arrives through the environment rather than being interpolated into
    # the script, which the injection test above insists on.
    assert 'git show "origin/$BASE_BRANCH' in body
    assert steps[guard]["env"].get("BASE_BRANCH")
    assert "sources/sources.json" in body
    # And it must fail rather than warn.
    assert "sys.exit(1)" in body
    assert "::error::" in body
    # Removals and additions both, since a stale branch shows up as either.
    assert "removed != claimed" in body and "added" in body


def test_a_scheduled_job_closes_its_last_notice_before_opening_another():
    """These run on a schedule and file an issue when they have something to
    say. Opening one per run and never closing one leaves a list that grows
    for ever and that nobody reads by the second week.

    For the roles report the previous one is superseded by definition: its
    roles are already in the database and the new one lists what arrived
    since. For a failure notice, a job that fails every night for a week is
    one problem, and seven identical issues make it look like seven.
    """
    for name in ("scan.yml", "validate.yml", "sync-skills.yml"):
        text = _text(name)
        creates = text.count("gh issue create")
        # scan.yml mentions the command once more inside a comment.
        if name == "scan.yml":
            creates -= 1
        closes = text.count("gh issue close")
        assert creates > 0, f"{name} files no issue"
        assert closes == creates, (
            f"{name} opens {creates} kind(s) of issue and closes {closes}")
        # Closing must never be able to fail the job: a fork with Issues
        # disabled has to keep scanning.
        for block in text.split("gh issue close")[1:]:
            head = block[:400]
            assert "|| true" in head or "2>/dev/null" in head, name


def test_the_close_step_searches_by_the_title_it_writes():
    """A search that does not match the title it files leaves the old issue
    open and looks like it worked."""
    import re
    for name in ("scan.yml", "validate.yml", "sync-skills.yml"):
        text = _text(name)
        searched = set(re.findall(r"""in:title "([^"]+)\"""", text))
        titled = set()
        for m in re.finditer(r"--title \"([^\"$]*)", text):
            t = m.group(1).strip()
            if t:
                titled.add(t)
        for s in searched:
            assert any(t.startswith(s.rstrip()) for t in titled), (
                f"{name}: searches for {s!r} but files {sorted(titled)!r}")
