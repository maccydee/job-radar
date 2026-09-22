"""`scan` makes the source list current itself, and says so either way.

The bug, in the maintainer's own words on 22 September 2026: his list was nine
days stale and the scan was "quietly missing roles". The tool knew. It printed
a note at the end of every run saying the list was old and that `git pull`
would fix it, and had been printing it for nine days. A note is not a fix, and
a tool that can see the problem and asks its reader to go and solve it has
chosen the one thing that does not work.

What matters here is not that the refresh happens. It is that every way it can
fail looks different from it working, because this repository's signature
defect is a failure that renders identically to a success, and a source-list
refresh is a wonderful place to reintroduce one: a truncated download parses
perfectly as a list with fewer employers on it, and the employers that fell
off look exactly like employers that do not exist.

So the tests below are mostly about the failures. No network is touched: the
single HTTP seam is substituted everywhere, and a stub that raises if it is
called is how "it did not even ask" gets asserted.
"""

import contextlib
import io
import json
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import cli, source_update              # noqa: E402
from jobradar import sources as src_mod              # noqa: E402
from jobradar.config import Config                   # noqa: E402


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def _list_json(n: int, checked: str, prefix: str = "Old") -> str:
    """A source list in the shipped shape, with `n` employers on it."""
    return json.dumps({
        "meta": {"version": 6, "checked": checked, "boards": n},
        "sources": [{"company": f"{prefix}{i}", "platform": "greenhouse",
                     "url": f"https://boards-api.greenhouse.io/v1/boards/{prefix}{i}/jobs"}
                    for i in range(n)],
    })


def _env(*, local_age: int = 30, local_n: int = 100):
    """A temp tree holding a bundled list of a chosen age, plus a state dir.

    Returns `(tmpdir, bundled_path, state_dir)`. Every test patches
    `sources.BUNDLED` at this path, so nothing here can read or write the real
    17,810-board list in the repository.
    """
    tmp = Path(tempfile.mkdtemp())
    bundled = tmp / "sources" / "sources.json"
    bundled.parent.mkdir(parents=True)
    bundled.write_text(_list_json(local_n, _days_ago(local_age)),
                       encoding="utf-8")
    state = tmp / "state"
    state.mkdir()
    return tmp, bundled, state


def _cfg(**kw) -> Config:
    return Config(titles_include=["engineer"], use_bundled_sources=True, **kw)


def _serves(status=200, body=None, etag='"abc"', log=None):
    """An HTTP stub. `log` collects every call, so "it never asked" is testable."""
    def get(url, tag="", *a, **k):
        if log is not None:
            log.append((url, tag))
        return status, (body.encode("utf-8") if body is not None else None), etag
    return get


def _never(url, tag="", *a, **k):
    raise AssertionError("a request was made when none should have been")


def _update(cfg, bundled, state, **kw):
    """`source_update.update` with the bundled list pointed at the temp tree.

    `repo_root` is the temp tree too, which has no `.git`, so the local-change
    detector answers "cannot tell" rather than running git against whatever
    checkout the suite happens to be sitting in.
    """
    kw.setdefault("repo_root", bundled.parent.parent)
    with mock.patch.object(src_mod, "BUNDLED", bundled):
        return source_update.update(cfg, state_dir=state, **kw)


# ---------------------------------------------------------------- the point


def test_a_stale_list_is_made_current_without_anyone_being_asked_to():
    """The whole feature. Nine days stale, and the next scan is not."""
    tmp, bundled, state = _env(local_age=9, local_n=100)
    fresh = _list_json(140, _days_ago(0), prefix="New")
    r = _update(_cfg(), bundled, state, http=_serves(200, fresh))

    assert r.state == "updated", r.message
    assert r.current is True
    assert "140" in r.message, r.message

    with mock.patch.object(src_mod, "BUNDLED", bundled):
        loaded = src_mod.load_file(src_mod.active_file(state_dir=state))
        assert len(loaded) == 140
        assert src_mod.age_days(state_dir=state) == 0


def test_the_tracked_list_is_never_written_even_on_a_successful_update():
    """`sources/sources.json` is tracked in a git checkout. Writing it would
    leave every user with a permanently dirty tree and a `git pull` that
    refuses to merge, and it would make it possible to destroy somebody's own
    edits. The downloaded copy goes beside the seen-set instead."""
    tmp, bundled, state = _env(local_age=30)
    before = bundled.read_bytes()
    _update(_cfg(), bundled, state,
            http=_serves(200, _list_json(200, _days_ago(0), prefix="New")))
    assert bundled.read_bytes() == before, "the shipped list was overwritten"
    assert (state / src_mod.UPDATED_NAME).is_file()


def test_the_newer_of_the_two_lists_is_the_one_that_loads():
    """One rule covers every case: whichever was CHECKED more recently.

    So a later `git pull` beats last month's download with nobody clearing a
    cache, and a local `validate --prune`, which stamps `checked` with today,
    keeps winning until upstream publishes something newer.
    """
    tmp, bundled, state = _env(local_age=30)
    downloaded = state / src_mod.UPDATED_NAME
    with mock.patch.object(src_mod, "BUNDLED", bundled):
        assert src_mod.active_file(state_dir=state) == bundled

        downloaded.write_text(_list_json(120, _days_ago(1)), encoding="utf-8")
        assert src_mod.active_file(state_dir=state) == downloaded

        # The pull case: the shipped list is now the newer of the two.
        bundled.write_text(_list_json(130, _days_ago(0)), encoding="utf-8")
        assert src_mod.active_file(state_dir=state) == bundled

        # A tie goes to the shipped one, which is the one a reader can see.
        downloaded.write_text(_list_json(120, _days_ago(0)), encoding="utf-8")
        assert src_mod.active_file(state_dir=state) == bundled


# ------------------------------------------------- failures that must look it


def test_no_failure_ever_claims_the_list_is_current():
    """The signature bug, asked of every way this can go wrong at once.

    `current` is what suppresses the "your list is old" note at the end of a
    scan. Any failure that set it would produce a run that read a stale list
    and said nothing about it, which is exactly what was already happening.
    """
    tmp, bundled, state = _env(local_age=20, local_n=100)
    short = _list_json(10, _days_ago(0), prefix="New")

    def boom(url, tag="", *a, **k):
        raise OSError("Name or service not known")

    cases = {
        "no network": boom,
        "refused": _serves(429, None),
        "not found": _serves(404, None),
        "server error": _serves(503, None),
        "not json": _serves(200, "<html>404</html>"),
        "truncated": _serves(200, short),
        "undated": _serves(200, json.dumps(
            {"meta": {}, "sources": [{"company": "A", "url": "https://a/x"}] * 0})),
    }
    for name, http in cases.items():
        # A fresh state dir each time, or the once-a-day cap answers for the
        # second case onwards and the test stops testing anything.
        _, bundled, state = _env(local_age=20, local_n=100)
        r = _update(_cfg(), bundled, state, http=http)
        assert r.current is False, f"{name} reported the list as current"
        assert r.message.strip(), f"{name} said nothing at all"
        assert r.state in ("failed", "refused"), f"{name} -> {r.state}"
        assert not (state / src_mod.UPDATED_NAME).exists(), \
            f"{name} installed something anyway"


def test_a_short_download_is_refused_rather_than_installed():
    """A list with fewer employers on it is not visibly broken. The same gate
    `tools/refresh_seed.py` uses, pointed the other way: there it stops a
    short build being published, here a short download being installed."""
    tmp, bundled, state = _env(local_age=20, local_n=100)
    r = _update(_cfg(), bundled, state,
                http=_serves(200, _list_json(10, _days_ago(0), prefix="New")))
    assert r.state == "refused"
    assert "10" in r.message and "100" in r.message, r.message
    assert "10%" in r.message, r.message

    # And the boundary is where it says it is: 80 of 100 is allowed through.
    tmp, bundled, state = _env(local_age=20, local_n=100)
    r = _update(_cfg(), bundled, state,
                http=_serves(200, _list_json(80, _days_ago(0), prefix="New")))
    assert r.state == "updated", r.message


def test_a_list_older_than_the_one_here_is_not_installed_over_it():
    """What a maintainer sees after `validate --prune`, which stamps `checked`
    with today. Upstream is then behind this machine and must not win."""
    # Ten days old, so stale enough to ask, and what comes back was checked
    # twenty days ago. The staleness gate lets the request happen; the date
    # comparison is what refuses the answer.
    tmp, bundled, state = _env(local_age=10, local_n=100)
    r = _update(_cfg(), bundled, state,
                http=_serves(200, _list_json(100, _days_ago(20))))
    assert r.state == "refused", r.message
    assert "older of the two" in r.message, r.message


def test_a_refusal_is_answered_once_and_not_retried():
    """Respect a no. One request, the code reported, and ask again tomorrow."""
    tmp, bundled, state = _env(local_age=20)
    log = []
    r = _update(_cfg(), bundled, state, http=_serves(429, None, log=log))
    assert len(log) == 1, f"{len(log)} requests for one refusal"
    assert r.state == "refused" and "429" in r.message


def test_a_cache_that_cannot_be_written_is_reported_not_swallowed():
    """A downloaded list that never reached disk is a scan reading the old
    one. If that printed nothing, the next run would look identical to a run
    that worked."""
    tmp, bundled, state = _env(local_age=20)

    def refuse(path, text):
        if str(path).endswith(src_mod.UPDATED_NAME):
            raise OSError("Read-only file system")
        return Path(path)

    with mock.patch.object(source_update, "atomic_write_text", refuse):
        r = _update(_cfg(), bundled, state,
                    http=_serves(200, _list_json(200, _days_ago(0), prefix="New")))
    assert r.state == "failed" and r.current is False
    assert "could not be saved" in r.message, r.message
    assert not (state / src_mod.UPDATED_NAME).exists()


# ------------------------------------------------------- when it must not ask


def test_a_list_that_is_still_fresh_is_not_fetched_at_all():
    """One request, to one host, only when the list is actually stale, and on
    the threshold that already exists rather than a new one."""
    tmp, bundled, state = _env(local_age=source_update.STALE_AFTER_DAYS - 1)
    r = _update(_cfg(), bundled, state, http=_never)
    assert r.state == "fresh"
    assert r.message == "", "a silent no-op said something"


def test_one_request_a_day_at_most_even_when_it_keeps_failing():
    """An offline laptop scanned six times must not put six failed requests on
    somebody else's CDN. The staleness gate still decides whether to ask at
    all; this only caps how often."""
    tmp, bundled, state = _env(local_age=20)
    log = []
    first = _update(_cfg(), bundled, state, http=_serves(503, None, log=log))
    assert first.state == "refused" and len(log) == 1
    again = _update(_cfg(), bundled, state, http=_never)
    assert again.state == "tried-today"
    assert again.current is False
    assert "HTTP 503" in again.message, again.message


def test_an_unchanged_list_is_confirmed_current_rather_than_called_stale():
    """Eight days old and eight days behind are different facts. A 304 says
    the copy here IS the published one, and a run that has learnt that must
    not then print a warning telling its reader the opposite."""
    tmp, bundled, state = _env(local_age=20)
    r = _update(_cfg(), bundled, state, http=_serves(304, None, etag='"z"'))
    assert r.state == "confirmed" and r.current is True

    said = []
    with mock.patch.object(src_mod, "age_days", return_value=20), \
            mock.patch.object(cli, "_say", said.append):
        cli._staleness_note(_cfg(), r)
    assert said == [], said

    # And it is remembered, so the next run today does not ask again.
    again = _update(_cfg(), bundled, state, http=_never)
    assert again.state == "confirmed" and again.current is True


def test_local_changes_to_the_tracked_list_stop_the_update():
    """Nothing here writes `sources/sources.json`, so a hand edit is safe on
    disk either way. What this stops is the subtler version, where the edit
    survives and is quietly ignored because a download loads instead."""
    tmp, bundled, state = _env(local_age=20)
    with mock.patch.object(source_update, "_locally_modified", lambda root: True):
        r = _update(_cfg(), bundled, state, http=_never)
    assert r.state == "local-changes"
    assert r.current is False
    assert "local changes" in r.message and "20 days ago" in r.message


def test_no_git_is_not_a_failure():
    """A pip install has no checkout to ask. That answer is "cannot tell", and
    it must not stop the update, because those are the users who can never
    make their list current any other way."""
    tmp, bundled, state = _env(local_age=20)
    assert source_update._locally_modified(tmp) is None
    r = _update(_cfg(), bundled, state,
                http=_serves(200, _list_json(200, _days_ago(0), prefix="New")))
    assert r.state == "updated", r.message


def test_both_ways_off_are_honoured_and_each_names_itself():
    """A reader told only "updates are off" goes hunting in the config for a
    setting that may not be the one that is off."""
    tmp, bundled, state = _env(local_age=20)
    r = _update(_cfg(), bundled, state, off_because="--no-source-update",
                http=_never)
    assert r.state == "off" and "--no-source-update" in r.message
    assert "20 days ago" in r.message
    assert r.current is False

    r = _update(_cfg(), bundled, state,
                off_because="sources.auto_update: false in your config",
                http=_never)
    assert r.state == "off" and "sources.auto_update" in r.message


def test_a_config_that_does_not_use_the_bundled_list_is_left_alone():
    tmp, bundled, state = _env(local_age=20)
    r = _update(Config(titles_include=["x"], use_bundled_sources=False),
                bundled, state, http=_never)
    assert r.state == "not-bundled" and r.message == ""


# ------------------------------------------------------------ what scan says


def test_the_note_at_the_end_no_longer_hands_the_job_back():
    """It used to end with "`git pull` gets you boards that have moved", which
    is the tool asking its reader to do the tool's job, and it had been asking
    for nine days while the scan went on missing roles."""
    said = []
    failed = source_update.Result("failed", "  it broke", days=23)
    with mock.patch.object(src_mod, "age_days", return_value=23), \
            mock.patch.object(cli, "_say", said.append):
        cli._staleness_note(_cfg(), failed)
    text = "\n".join(said)
    assert text.strip(), "a scan on a 23-day-old list said nothing"
    assert "23 days ago" in text
    assert "git pull" not in text, text
    assert "tries again" in text, text


def test_the_note_says_which_of_the_two_things_went_wrong():
    """"Updates are off" and "the update failed" need different next steps,
    and a single wording for both would give the wrong one half the time."""
    said = []
    off = source_update.Result("off", "  off", days=23)
    with mock.patch.object(src_mod, "age_days", return_value=23), \
            mock.patch.object(cli, "_say", said.append):
        cli._staleness_note(_cfg(), off)
    text = "\n".join(said)
    assert "--no-source-update" in text and "auto_update" in text, text


def test_the_daily_nudge_no_longer_tells_you_to_pull():
    """`list`, `serve` and `rank` do not scan, so the nudge points at the
    command that now fixes it rather than at a git command that is wrong for
    every pip install."""
    said = []
    with mock.patch.object(src_mod, "age_days", return_value=23), \
            mock.patch.object(cli, "_say", said.append):
        cli._daily_sync_nudge(_cfg(), ":memory:")
    assert len(said) == 1 and "git pull" not in said[0], said
    assert "job-radar scan" in said[0]


# ------------------------------------------------------------- end to end


def _scan_config(tmp: Path) -> Path:
    cfg = tmp / "config.yaml"
    cfg.write_text("titles:\n  include: [engineer]\n"
                   "sources:\n  use_bundled: true\n", encoding="utf-8")
    return cfg


def test_a_scan_reads_the_list_it_just_downloaded_not_the_one_it_started_with():
    """The ordering IS the feature. An update that lands after the sources are
    loaded changes nothing about the run it reports success in, which is this
    repository's signature failure wearing a new hat.

    No board is contacted: `fetch_all` is substituted, and what it was handed
    is the assertion.
    """
    tmp, bundled, _ = _env(local_age=30, local_n=100)
    cfg = _scan_config(tmp)
    state = tmp / "scan-state" / "seen.json"
    asked: list = []

    def fake_fetch(srcs, **kw):
        from jobradar.fetch import Result
        asked.extend(srcs)
        return [Result(source=s, payload={"jobs": []}, status=200) for s in srcs]

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), \
            mock.patch.object(src_mod, "BUNDLED", bundled), \
            mock.patch.object(source_update, "_http_get",
                              _serves(200, _list_json(150, _days_ago(0),
                                                      prefix="New"))), \
            mock.patch.object(source_update, "_locally_modified",
                              lambda root: None), \
            mock.patch("jobradar.cli.fetch_all", side_effect=fake_fetch):
        cli.main(["-c", str(cfg), "scan", "--no-enrich", "--no-caffeine",
                  "--no-open", "--db", ":memory:", "--out", str(tmp / "out"),
                  "--state", str(state)])

    names = {s.company for s in asked}
    assert len(names) == 150, f"{len(names)} sources read, so the old list won"
    assert all(n.startswith("New") for n in names), sorted(names)[:3]
    assert "Source list updated" in buf.getvalue(), buf.getvalue()


def test_the_flag_turns_it_off_for_one_run_and_the_scan_still_runs():
    """A way off, and a run that still works without it."""
    tmp, bundled, _ = _env(local_age=30, local_n=100)
    cfg = _scan_config(tmp)
    state = tmp / "scan-state" / "seen.json"
    asked: list = []

    def fake_fetch(srcs, **kw):
        from jobradar.fetch import Result
        asked.extend(srcs)
        return [Result(source=s, payload={"jobs": []}, status=200) for s in srcs]

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), \
            mock.patch.object(src_mod, "BUNDLED", bundled), \
            mock.patch.object(source_update, "_http_get", _never), \
            mock.patch("jobradar.cli.fetch_all", side_effect=fake_fetch):
        rc = cli.main(["-c", str(cfg), "scan", "--no-source-update",
                       "--no-enrich", "--no-caffeine", "--no-open",
                       "--db", ":memory:", "--out", str(tmp / "out"),
                       "--state", str(state)])
    out = buf.getvalue()
    assert rc == 0, out
    assert len(asked) == 100, out
    assert "--no-source-update" in out, out


if __name__ == "__main__":
    # BaseException, not Exception. `argparse` exits with SystemExit on an
    # unknown flag, and SystemExit is not an Exception, so catching the
    # narrower one ends the run mid-file with no failure line and no summary.
    # That has cost this suite a whole file once already; see the note in
    # tests/run_all.py.
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except BaseException as e:                 # noqa: BLE001
                fails += 1
                print(f"FAIL {name}: {type(e).__name__}: {e}")
    raise SystemExit(1 if fails else 0)
