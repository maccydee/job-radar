"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import yaml

from . import adapters, output, sources as src_mod
import webbrowser

from . import serve as serve_mod
from .awake import describe, keep_awake
from .config import Config, ConfigError, load as load_cfg
from .discover import discover as run_discover, prunable as row_prunable, validate_source
from . import fetch as fetch_defaults
from .fetch import (HostLimiter, detect_throttling, fetch_all,
                    interleave_by_host, pace_this_thread,
                    pinned_to_one_page)
from .models import Source
from .screen import run as screen_run, _countries_in
from .state import State, atomic_write_text


def _say(msg: str = "") -> None:
    print(msg, flush=True)


def _load_sources(cfg: Config) -> list[Source]:
    """The configured sources, saying out loud which ones were unusable.

    A templated URL asking for a placeholder this tool cannot supply used to
    raise straight out of `sources.load`, so one bad line in `sources.extra`
    stopped every command that reads the list. It is now skipped, and skipping
    something silently is the other half of that bug, so it is named here.
    """
    problems: list = []
    srcs = src_mod.load(cfg, problems=problems)
    for company, why in problems[:10]:
        _say(f"  ! skipped {company}: {why}")
    if len(problems) > 10:
        _say(f"  ! and {len(problems) - 10} more source(s) with URLs that "
             f"cannot be filled in")
    return srcs


# ------------------------------------------------------------- pacing
def pacing_floors(srcs, limiter=None) -> list[tuple[float, str, int, float]]:
    """How long each paced host takes on its own clock, worst first.

    `(seconds, host, boards, gap)`. This is a floor the scan cannot beat
    however many workers it runs, because every request to one host queues
    behind that host's own gap. It exists because a board count hides it
    completely: apply.workable.com is 12% of the bundled list and 100% of the
    fifty minutes, purely because it is the one busy host paced below the
    default rate.

    Two honesty notes, both of which make this a LOWER bound rather than an
    estimate.

    Boards, not requests. A paged board costs several requests to its own
    host -- Workday walks up to ten pages per search term -- and none of that
    is counted here. It happens not to distort the answer, because the hosts
    with enough boards to matter are Greenhouse, Ashby, Workable,
    SmartRecruiters, Jobvite and the two Lever deployments, and every one of
    those is one request per board. The paged platforms put one board on each
    host, so their extra requests are spread over thousands of clocks rather
    than queued behind one.

    Hosts holding a single board are left out, and on the bundled list that is
    the overwhelming majority of them. A gap only ever delays a SECOND request
    to the same host, so on a host asked once the rate is a number that is
    never consulted, and those boards are limited by `fetch.concurrency`
    alone. That is why the two dials are not interchangeable, and why neither
    can be tuned by looking at the total.
    """
    limiter = limiter or HostLimiter()
    boards: dict[str, int] = {}
    for s in srcs:
        host = urlparse(s.url).netloc
        boards[host] = boards.get(host, 0) + 1
    out = []
    for host, n in boards.items():
        gap = limiter.gap_for(host)
        # `n * gap` rather than `(n - 1) * gap`, to agree with
        # `tools/bench_fetch.py --platforms`, which is where the 49.9 and 22.7
        # minute figures in the history came from. The difference is one gap.
        if gap > 0 and n > 1:
            out.append((n * gap, host, n, gap))
    out.sort(reverse=True)
    return out


# ---------------------------------------------------------------- scan
def out_dir_problem(outdir: Path) -> str:
    """Why nothing can be written into `outdir`, or "" if it is fine.

    Called before the first request, because the answer does not change during
    the scan and finding it out at the end is the worst possible moment. A
    full run is about 77 minutes and it ended in a `PermissionError` traceback
    at `atomic_write_text`, for a directory that was already unwritable when
    the command was typed. Nothing about the network, the boards or the
    filters was needed to know that.

    The probe is a real file rather than `os.access`, because a real file is
    what `atomic_write_text` is going to create: it writes `.index.html.NNN.
    tmp` alongside the target and renames it, so write permission on the
    DIRECTORY is the thing that matters and read-only-ness of any existing
    file is not. `os.access` also lies under sudo and on network mounts, and a
    pre-flight check that says yes and then fails is worse than none.
    """
    if outdir.exists() and not outdir.is_dir():
        return (f"output.dir is {outdir}, which is a file, not a directory.\n"
                f"The scan writes index.html and roles.json into it.")
    try:
        outdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return (f"Cannot create the output directory {outdir}: {exc}\n"
                f"Set `output.dir` in your config, or pass `-o` a path you "
                f"can write.")
    probe = outdir / f".job-radar-write-test.{os.getpid()}"
    try:
        probe.write_text("", encoding="utf-8")
    except OSError as exc:
        return (f"Nothing can be written into the output directory "
                f"{outdir}: {exc}\n"
                f"That is where index.html and roles.json go, so the scan "
                f"would spend an hour reading boards and then fail at the "
                f"last step. Set `output.dir` in your config, or pass `-o` a "
                f"path you can write.")
    try:
        probe.unlink()
    except OSError:
        pass          # writable is what was being asked; tidiness is a bonus
    return ""


def _mins(m: float) -> str:
    """A duration a person can act on.

    "about 0 minutes" is what `{:.0f}` prints for anything under thirty
    seconds, and it reads as a bug rather than as fast.
    """
    if m < 1:
        return "under a minute"
    if m < 90:
        return f"about {m:.0f} minute{'s' if round(m) != 1 else ''}"
    return f"about {m / 60:.1f} hours"


def _phase_minutes(group) -> float:
    """Roughly how long a pass will take, from the rates it will be paced at.

    Derived rather than written down, so raising a host's rate moves the
    estimate without anyone remembering to edit a number. The old estimate was
    a constant that said 40 minutes while the scan printed a different figure
    on the next line.

    A pass is bounded by whichever is worse: the slowest single host's queue,
    or the machine getting through the work at all. 0.48s is the measured mean
    request time across 41 hosts.
    """
    from collections import Counter
    from urllib.parse import urlparse

    from .fetch import DEFAULT_CONCURRENCY, DEFAULT_PER_HOST_RPS, PER_HOST_RPS

    worst = 0.0
    for host, n in Counter(urlparse(s.url).netloc for s in group).items():
        worst = max(worst, n / PER_HOST_RPS.get(host, DEFAULT_PER_HOST_RPS) / 60)
    return max(worst, len(group) * 0.48 / DEFAULT_CONCURRENCY / 60)


def _flush_phase(con, cfg, jobs, args, run=None) -> int:
    """Store and render what the scan has so far, between passes.

    Deliberately quiet and deliberately partial. It screens and writes what
    has arrived, so the dashboard is usable while the slow passes run, and it
    says nothing: the counts belong to the summary at the end, and four
    "N new roles" lines for one scan would read as four scans.

    Nothing here decides what is new. `run` is the scan's own run number, so a
    role found in pass one and a role found in pass four are stamped the same
    and are equally new to the same run. It is passed in rather than read off
    the counter here, because a second scan finishing between two of these
    passes moves that counter underneath us.
    """
    from . import store
    from .output import html as html_mod

    kept, _ = screen_run(list(jobs), cfg)
    if not kept:
        return 0
    try:
        store.upsert_roles(con, kept, run=run)
        con.commit()
        outdir = Path(args.out or cfg.out_dir)
        if "html" in cfg.formats:
            html_mod.write(outdir / "index.html", new=[], seen=kept, dropped={},
                           sources_ok=0, sources_total=0, throttled=[],
                           postings=len(jobs))
        return len(kept)
    except Exception:
        # A dashboard that could not be refreshed mid-scan is not a reason to
        # lose the scan. The end of the run writes it properly either way.
        # Reported as zero rather than as the number we meant to write, so a
        # failed flush cannot be announced as roles the user can go and read.
        return 0


def cmd_scan(args) -> int:
    cfg = load_cfg(args.config)
    # First, and before a single request. See `out_dir_problem`: this is the
    # one failure the scan used to discover 77 minutes in, having already done
    # all of the work and every bit of the asking-other-people's-servers.
    # A dry run is exempt because it deliberately writes nothing here, and a
    # config with no formats at all writes nothing either; refusing those
    # would be inventing a failure rather than reporting one.
    if not args.dry_run and cfg.formats:
        why = out_dir_problem(Path(args.out or cfg.out_dir))
        if why:
            _say(why)
            return 1
    srcs = _load_sources(cfg)
    # Whether the limit actually cut anything, rather than merely whether one
    # was asked for. `--limit 20000` against a 13,440-source config read every
    # one of them and still announced "only 20000 sources were read".
    all_srcs = len(srcs)
    if args.limit and args.limit < all_srcs:
        # Spread across the list, not the first N of it. The bundled list is
        # grouped by platform, so a head slice of any size up to the size of
        # the first group reads ONE platform: `scan --limit 300`, which the
        # README offers as the quick look, read 300 Ashby boards and touched
        # no Greenhouse, Workable, iCIMS, Workday or Personio board at all.
        # Personio is where the European employers are, so the recommended
        # first look was also the least representative slice of the list for
        # anyone outside the US. A stride is still deterministic, so repeating
        # the same command reads the same boards.
        step = all_srcs // args.limit
        srcs = srcs[::step][: args.limit] if step > 1 else srcs[: args.limit]
    truncated = len(srcs) < all_srcs
    if not srcs:
        _say("No sources. Run `job-radar setup` or check sources.use_bundled.")
        return 1

    state = State(Path(args.state) if args.state else None)
    # A config written before per-host pacing existed will still be carrying
    # the old advice to keep this number tiny, and nothing else would ever tell
    # its owner that the advice changed. At four workers against seventeen
    # thousand sources that setting is worth more than an hour a scan, and it
    # buys no politeness now that each host is paced on its own clock.
    if cfg.concurrency < fetch_defaults.DEFAULT_CONCURRENCY and len(srcs) > 500:
        _say(f"  ! concurrency is {cfg.concurrency}. Each host is now paced "
             f"separately, so this number only sets how many DIFFERENT boards "
             f"are read at once, and {len(srcs):,} sources at "
             f"{cfg.concurrency} is mostly waiting. "
             f"`fetch.concurrency: {fetch_defaults.DEFAULT_CONCURRENCY}` in "
             f"{cfg.path or 'your config'} is the new default.")

    # Say where the time goes before spending it, not after. The scan has two
    # floors and cannot beat the larger: one host's own clock, and the machine
    # divided by the workers. Only the first can be known before the run, and
    # it is the one nobody would guess, because it is not proportional to the
    # board count -- 12% of the bundled list has been 100% of the wall clock.
    #
    # It is also the number that decides whether raising `fetch.concurrency`
    # would do anything at all. While a paced host sets the floor, more
    # workers only park more of them in `HostLimiter.wait`.
    floors = pacing_floors(srcs)
    if floors and floors[0][0] >= 120:
        secs, host, n, gap = floors[0]
        paced = sum(1 for f in floors)
        on_paced = sum(f[2] for f in floors)
        _say(f"  the floor is {host}: {n:,} boards at "
             f"{1 / gap:.3g}/s is {secs / 60:.0f} min on that host alone, "
             f"whatever concurrency is set to.")
        _say(f"  {on_paced:,} of {len(srcs):,} sources sit on the {paced} host(s) "
             f"where pacing applies at all; the rest hold one board each and "
             f"are limited by concurrency ({cfg.concurrency}), not by rate.")

    done = {"n": 0}
    all_jobs: list = []
    counts: dict[str, int] = {}
    absorbed: set = set()
    ok = 0

    def absorb(res):
        """Turn one fetched source into jobs. Called at most once per result.

        Idempotent by identity, because it runs from two places: `tick`, while
        the fetch is still going, and a sweep afterwards for anything `tick`
        never saw. It must not be possible for a result to be counted twice --
        `ok` and `all_jobs` would both inflate, and inflating the posting count
        is the shape of bug this file keeps finding.
        """
        nonlocal ok
        if id(res) in absorbed or not res.ok:
            return
        absorbed.add(id(res))
        ok += 1
        jobs = adapters.parse(res.payload, res.source)
        for j in jobs:
            j.sector = j.sector or res.source.sector
            # The posting's own location beats the board's tag. A board is
            # tagged with where its vacancies usually are, which is a fair
            # default and a bad override: Homebase's board is tagged UK
            # because that is a UK retailer, and a genuine Toronto vacancy on
            # it was being stored as UK. The tag is only a fallback for a
            # posting that names nowhere, and it is only used when it names
            # exactly one country, since "multiple" is not one.
            if not j.country:
                here = _countries_in(j.location or "")
                tag = res.source.country or ""
                # One spelling, defined in sources.py and normalised as the
                # list is loaded. This used to accept both `multi` and
                # `multiple` because the shipped list held both, which meant
                # neither was the right one and the next consumer would handle
                # whichever it happened to meet.
                if tag in src_mod.NON_COUNTRY_TAGS:
                    tag = ""            # not a country, never store it as one
                if len(here) == 1:
                    j.country = here.pop()
                elif here:
                    # Several countries named. The board's tag is only usable
                    # if it is one of them: "London / New York" on a UK board
                    # really is partly UK, "Berlin / Paris" is not.
                    j.country = tag if tag in here else ""
                else:
                    j.country = tag
        counts[res.source.key] = len(jobs)
        all_jobs.extend(jobs)

    def tick(res):
        """Count the source, and parse it while the fetch is still running.

        The parsing used to be a second pass over `results` after `fetch_all`
        had returned. Moving it in here is worth about two minutes of wall
        clock on a full scan and changes nothing else, because `fetch_all`
        calls this from its own `as_completed` loop: same thread, same
        completion order, same one-call-per-result. The old second pass ran on
        that same thread too, just later.

        What it buys is overlap. The scan's floor is not this machine, it is
        apply.workable.com's pacing -- 2,094 boards at 0.7 requests a second
        is 50 minutes on its own, and measured across a 179-board sample only
        about five of sixteen workers are busy at any moment. So the thread
        running this callback spends most of the run blocked in
        `as_completed` with nothing to do, and parsing roughly 480,000
        postings at 262 microseconds each is ~126 seconds of work that now
        happens inside time that was already being spent waiting.

        Deliberately only the parsing. Screening cannot move here: it starts
        with `dedupe` across the whole set, so it has nothing to do until
        every source is in.
        """
        done["n"] += 1
        if done["n"] % 25 == 0:
            _say(f"  {done['n']}/{len(srcs)}")
        absorb(res)

    # Derived, not written down. This said "only the first 6" while
    # `MAX_KEYWORD_TITLES` was 12, and the note ten lines below reads the
    # constant -- so a 25-title config printed "the first 6" and "your first
    # 12" one line apart, and neither the user nor this file could say which
    # was true. Order does matter, so the number has to be the real one.
    if len(cfg.titles_include) > src_mod.MAX_KEYWORD_TITLES:
        _say(f"  note: only the first {src_mod.MAX_KEYWORD_TITLES} of your "
             f"{len(cfg.titles_include)} titles are used as search terms "
             f"(Workday uses 3). Order matters.")
    # Reed is the one source that needs a credential, and without one it can
    # only 401. Say so here, once, by name: buried in the list of sources that
    # "could not be read" it looks like a broken board rather than a two
    # minute signup.
    if not cfg.reed_api_key and any(s.platform == "reed" for s in srcs):
        _say("  ! Reed is in your sources but there is no API key, so it will "
             "be skipped.")
        _say("    Free key: https://www.reed.co.uk/developers/jobseeker  "
             "Then set sources.reed_api_key or $REED_API_KEY.")
    # Same for Adzuna, which without credentials can only answer 400 with an
    # HTML error page. That reads as a broken source, not as a signup.
    if (not (cfg.adzuna_app_id and cfg.adzuna_app_key)
            and any(s.platform == "adzuna" for s in srcs)):
        _say("  ! Adzuna is in your sources but there are no credentials, so "
             "it will be skipped.")
        _say("    Free app_id and app_key: https://developer.adzuna.com/signup  "
             "Then set sources.adzuna_app_id and sources.adzuna_app_key, or "
             "$ADZUNA_APP_ID and $ADZUNA_APP_KEY.")

    # No silent truncation. A keyword platform is searched with the first
    # MAX_KEYWORD_TITLES titles, and a config with more than that was quietly
    # having the rest ignored: LinkedIn, NHS Jobs, Reed, Adzuna and the
    # Workable search all never looked for them.
    _skipped = src_mod.dropped_titles(cfg.titles_include)
    if _skipped:
        _say(f"  note: the keyword searches use your first "
             f"{src_mod.MAX_KEYWORD_TITLES} titles, so these are not searched "
             f"for there: {', '.join(_skipped)}")
        _say("  they are still matched against every employer board.")

    # Read in passes, fastest first, and say so before spending an hour.
    #
    # Half the roles are on hosts nobody rate limits and arrive in about five
    # minutes; the last 6% are behind Workable and cost fifty. Read as one
    # lump, none of it is usable until all of it is, and the way people
    # actually use this is to set it up, look at the dashboard, apply to
    # something and shut the laptop. A scan that only pays out at the end pays
    # out to nobody.
    #
    # Every phase still runs. A posting can appear and be gone inside a week,
    # so skipping one is a role never seen, which is worse than a slow scan.
    phases = src_mod.in_phases(srcs)
    # Numbered by position in this run. A `--limit` run can hold only phase 2,
    # and printing its global id gives "Pass 2 of 1".
    est = [(i, label, group, _phase_minutes(group))
           for i, (_, label, group) in enumerate(phases, 1)]
    total = sum(m for _, _, _, m in est)
    _say(f"Reading {len(srcs):,} sources in {len(est)} "
         f"pass{'es' if len(est) != 1 else ''}, {_mins(total)} in total.")
    for n, label, group, mins in est:
        _say(f"  {n}. {label:<24}{len(group):>6} sources   {_mins(mins)}")
    _say("The dashboard is worth opening after the first pass; the rest fill "
         "in behind it.")

    from . import store
    # A dry run touches no file it was not pointed at. It used to create the
    # database anyway, empty, purely because connecting creates it, which made
    # "this writes nothing" untrue in the one mode people use to check exactly
    # that before trusting the tool.
    con = store.connect(":memory:" if args.dry_run else args.db)
    # The legacy import follows the database, not the working directory.
    # `store.migrate(con)` resolved both of its sources against the cwd, so a
    # scan started in the repo with `--db /tmp/scratch.db` still read this
    # directory's state/seen.json and applications.local.yaml and copied 1,526
    # roles and a real application history into the scratch file. `--db` reads
    # as isolation and was not one, and the result is somebody's job search in
    # a temp directory they will not think to clear.
    #
    # A database that is the configured one keeps the old behaviour, because
    # that is the upgrade path this function exists for.
    # The run number this scan writes, taken once and used for every role it
    # stamps and every question it asks about newness.
    #
    # It used to be re-derived from the counter at each of those moments, as
    # "whatever runs is now, plus one". Two scans over the same database do
    # not take turns: a cron scan overlapping a manual one had the second scan
    # stamp its rows `first_run=1`, then read the counter again after the
    # first scan had bumped it, and go looking for `first_run=2`. Both scans
    # found the same 250 roles on a fresh database; the first said "first
    # scan, all new" and the second said `250 match your config, 0 new`. No
    # corruption, no lock error, no warning -- just the one sentence this
    # tool exists to never print, on 250 roles that were every one of them
    # new.
    #
    # Pinning it makes the two scans agree instead of contradicting each
    # other, which is why this is a fix rather than a warning: both are
    # honestly reporting what was new when THEY started, and on a fresh
    # database that is all of it. `store.bump_runs` is separately atomic now,
    # so the counter itself cannot lose one of the two increments either.
    this_run = store.current_run(con) + 1
    own_db = not args.db or Path(args.db) == store.DEFAULT_PATH
    mig = ({"roles": 0, "statuses": 0} if args.dry_run else
           store.migrate(con,
                         state_path=str(state.path) if own_db else "",
                         apps_path=None if own_db else ""))
    if mig["roles"] or mig["statuses"]:
        _say(f"  migrated {mig['roles']} roles and {mig['statuses']} statuses "
             f"into the database")

    # Opened here rather than after the fetch loop, which is where it used to
    # live. Passes flush to the database between themselves, so a scan that
    # writes at pass one cannot wait for a connection made at the end of pass
    # four. The guard that hid this was `n < len(est)`: on a one-pass run the
    # flush was skipped, and every test used `--limit`, so a real multi-pass
    # scan would have raised UnboundLocalError on the first flush.

    # Hold the machine awake for the run, and be honest about what that does.
    # It stops an idle laptop napping; it does not survive the lid closing.
    results = []
    with keep_awake("job-radar is scanning",
                    enabled=not args.no_caffeine) as awake:
        _say(describe(awake.held) + "\n")
        for n, label, group, mins in est:
            _say(f"Pass {n} of {len(est)}, {label}: {len(group):,} sources, "
                 f"{_mins(mins)}.")
            results += fetch_all(
                group,
                # Beside the seen-set, so it survives the run that learned it.
                # A host answering "not for another 23 hours" was being asked
                # again by the very next scan, because the answer lived only
                # in the process that heard it.
                blocks_path=Path(state.path).parent / "host-blocks.json",
                concurrency=cfg.concurrency, timeout=cfg.timeout,
                retries=cfg.retries, user_agent=cfg.user_agent,
                search_terms=cfg.titles_include,
                api_keys={"reed": cfg.reed_api_key,
                          "adzuna_app_id": cfg.adzuna_app_id,
                          "adzuna_app_key": cfg.adzuna_app_key}, on_result=tick,
            )
            # Written and rendered at the end of every pass, not only the
            # last, because a dashboard that is usable at five minutes is the
            # whole point of reading in passes at all.
            #
            # The flush has to happen before the open, and on a one-pass run
            # (`--limit`, or a config that only reaches fast hosts) there is
            # no "next pass" to flush for. Guarding both on `n < len(est)`
            # opened a dashboard against a database this scan had not written
            # to yet: an empty page that looks exactly like a scan that found
            # nothing.
            ready = 0
            if not args.dry_run and (n < len(est) or n == 1):
                ready = _flush_phase(con, cfg, all_jobs, args, run=this_run)

            if args.dry_run:
                pass
            elif n == 1 and not args.no_open:
                # Opened after the FIRST pass, not at the end of the scan.
                # Five minutes in there are enough roles to work through, and
                # another seventy minutes of reading is no reason to sit
                # watching a counter. The server is detached, so it stays up
                # when the scan finishes or is interrupted.
                # `--docs` belongs to `serve`, not to `scan`, so there is
                # nothing to forward and the server falls back to its own
                # default. Reading `args.docs` here raised AttributeError on
                # every scan, which is the fourth time a namespace that does
                # not match the parser has shipped. `test_scan_open` now
                # checks this call against the real parser.
                url = serve_mod.open_in_background(
                    db_path=args.db, config_path=args.config)
                if url:
                    # Says what is actually on the page. Pass one can match
                    # nothing, and "with what pass 1 found" in front of an
                    # empty dashboard reads as a broken tool rather than as a
                    # scan that is still going.
                    what = (f"with {ready:,} role{'' if ready == 1 else 's'} "
                            f"from pass 1" if ready else
                            "though pass 1 matched nothing yet")
                    _say(f"  dashboard is up at {url} {what}."
                         + (" It fills in as the rest arrive."
                            if n < len(est) else ""))
                    webbrowser.open(url)
                elif serve_mod.already_serving():
                    # Say what is actually known, which is that the port is
                    # taken. `already_serving` is a TCP connect: it does not
                    # check that the thing answering is a job-radar dashboard,
                    # and certainly not that it is reading THIS database. A
                    # server left running against another `--db`, or anything
                    # else holding 8765, got the same sentence -- "your
                    # dashboard already has pass 1, refresh it" -- and sent
                    # the reader to somebody else's roles believing they were
                    # their own. That is a claim about a page nothing here has
                    # looked at, which is the failure that renders as a
                    # success.
                    _say("  127.0.0.1:8765 is already in use, so no dashboard "
                         "was started for this scan.")
                    _say("  If that is a job-radar dashboard on this database, "
                         "refresh it. Otherwise run `job-radar serve --port "
                         "<other>`.")
                else:
                    _say("  dashboard written. `job-radar serve` opens it.")
            elif n < len(est):
                _say(f"  dashboard updated; passes {n + 1} to {len(est)} "
                     f"still to come.")

    # `tick` has already parsed everything `fetch_all` handed it, so on the
    # real path this loop finds nothing to do and costs one set lookup per
    # source. It is not dead code: it is what keeps the parsing an OPTIMISATION
    # rather than a contract. A `fetch_all` that returns its results without
    # calling `on_result` is a perfectly reasonable thing to write -- the
    # parameter is optional and several tests stub exactly that -- and without
    # this sweep such a caller would silently scan zero postings and report
    # "Nothing matched" on a full board. `absorb` is idempotent, so anything
    # already taken in is skipped rather than double counted.
    for res in results:
        absorb(res)

    throttled = detect_throttling(results, counts, state.source_counts)

    _say(f"  {ok}/{len(srcs)} responded, {len(all_jobs):,} postings")
    # "responded" is not "worked". A board that answers with nothing looks
    # identical to one that is not being watched at all, which is how five
    # hand-added charity boards were reported as healthy.
    empty = [r.source.company for r in results
             if r.ok and counts.get(r.source.key, 0) == 0]
    if empty and len(empty) <= 12:
        _say(f"  ! {len(empty)} source(s) responded with no postings at all: "
             f"{', '.join(empty)}")
        _say("    Run `job-radar validate` to see whether they are dead or "
             "just unreadable.")
    elif empty:
        _say(f"  ! {len(empty)} sources responded with no postings at all. "
             f"Run `job-radar validate`.")
    if throttled:
        _say(f"  ! {len(throttled)} sources look throttled (returned nothing "
             f"but have before): {', '.join(throttled[:6])}")
    # Name the host, not just the boards. "2,094 sources look throttled" is a
    # list of employers to squint at; "apply.workable.com has blocked you for
    # another 15h" is the actual fact, and it is the one that tells you the
    # boards are unknown today rather than empty. Measured live: Workable
    # answered 429 with Retry-After 57841 after a scan aimed every worker at
    # it, and 2,094 boards came back with nothing for the rest of the run.
    blocks: dict[str, int] = {}
    for res in results:
        if not res.throttled:
            continue
        host = urlparse(res.source.url).netloc
        m = re.search(r"(\d+)s", res.error or "")
        if m:
            blocks[host] = max(blocks.get(host, 0), int(m.group(1)))
    for host, secs in sorted(blocks.items(), key=lambda kv: -kv[1]):
        n = sum(1 for r in results
                if r.throttled and urlparse(r.source.url).netloc == host)
        # Formatted rather than hard-coded to hours: the breaker's own block
        # is five minutes, and "0h 5m" reads like a bug in the tool.
        gap = (f"{secs // 3600}h {secs % 3600 // 60}m" if secs >= 3600
               else f"{secs // 60}m {secs % 60}s" if secs >= 60
               else f"{secs}s")
        _say(f"  ! {host} is rate-limiting this connection, so the next "
             f"request there waits {gap}. "
             f"{n} source(s) there are UNKNOWN today, not empty. They are "
             f"left alone rather than recorded as having no jobs.")
    # The other silent failure, and the harder one to see. A throttled source
    # returns nothing and at least looks wrong; a source that returns exactly
    # one page looks perfectly healthy. Tesco's Avature board returned 10 of
    # "999+" and `validate` called it live.
    pinned = pinned_to_one_page(counts, srcs)
    if pinned:
        _say(f"  ! {len(pinned)} source(s) returned exactly one page of their "
             f"platform, which can mean paging stopped early rather than that "
             f"the board is that size: {', '.join(pinned[:6])}")

    # A heuristic above, a fact here. `pinned_to_one_page` guesses from the
    # count; this is the pager itself saying it stopped because it ran out of
    # allowance rather than because the board ran out of postings. Every paged
    # fetcher has a cap, because a broken stop condition with no cap behind it
    # is an infinite loop, and until now every one of them was silent about
    # hitting it: the first 200 of 1,055 came back looking exactly like a
    # complete answer.
    capped = sorted({r.source.company for r in results if r.truncated})
    if capped:
        _say(f"  ! {len(capped)} source(s) had more to give and were cut off at "
             f"the page limit, so these are incomplete: {', '.join(capped[:6])}")

    kept, dropped = screen_run(all_jobs, cfg)

    # The database is the source of truth for what you already did about a
    # role. It beats whatever the scanner thinks of it today.

    # A dry run must not touch the database. It used to insert every role and
    # increment the run counter, so trying the tool out once silently spent
    # the newness of everything it saw: the next real scan reported those
    # roles as already known. The help text promised the opposite.
    if args.dry_run:
        new_ids = set()
    else:
        store.upsert_roles(con, kept, run=this_run)
        new_ids = store.new_since_last_run(
            con, [j.uid for j in kept], run=this_run)

    settled = store.settled_uids(con)
    hidden = [j for j in kept if j.uid in settled]
    kept = [j for j in kept if j.uid not in settled]

    # Carry each role's status onto the job so the dashboard can show it.
    for j in kept:
        row = con.execute("SELECT status FROM role_state WHERE uid=?",
                          (j.uid,)).fetchone()
        j.app_status = row["status"] if row and row["status"] != "new" else ""
    if hidden:
        _say(f"  {len(hidden)} settled and hidden")

    # Read off this scan's own pinned run number rather than off the counter,
    # which by now another scan may have bumped. Reading the counter here also
    # had to happen BEFORE `bump_runs` or it was always False and the
    # first-run message below was dead code: day one printed "0 new" and was
    # indistinguishable from a no-change repeat. Pinning removes both
    # orderings as things anybody has to remember.
    first_run = this_run == 1
    if not args.dry_run:
        store.bump_runs(con)
    # Rendered from the BOARD, not from this run's finds.
    #
    # The static page took whatever the scan matched while `serve` reads the
    # database, so the two disagreed exactly where it mattered. After a seed
    # load of 267 roles and a `scan --limit 400`, `out/index.html` was headed
    # "15 roles worth a look" and `job-radar serve` showed 270, with nothing
    # on either explaining the gap. A reader wants their board, not the slice
    # of it one command happened to touch.
    #
    # A dry run has written nothing, so it still shows what it found: there
    # is no board to read that would reflect this run.
    # Read AFTER this run's own cleanup, not before it.
    #
    # It was read here, and `repair_smartrecruiters_urls`, `merge_duplicates`
    # and the enrichment pass all run fifty lines below, so the page was
    # written from a snapshot taken before the scan had finished tidying. It
    # showed roles that same run had closed, showed a merged duplicate twice
    # with identical company and title, and carried a uid that no longer
    # existed in the database. The counts disagreed on the same minute:
    # index.html 1066, list 1063.
    #
    # Deferred to `_board_now`, called once the cleanup is done.
    board = None
    # What THIS SCAN found that is new, which is what the lines below report.
    # Distinct from the board-scale `new` built after the cleanup, which is
    # what the page and roles.json describe.
    new_now = [j for j in kept if j.uid in new_ids]
    if args.dry_run and kept:
        _say(f"  {len(kept)} match your config. Dry run, so nothing was "
             f"recorded and none can be marked new.")
    elif first_run and kept:
        # "0 new" on a first run reads as "we found nothing", when in fact
        # everything is new and there is nothing to compare against yet.
        #
        # The previous wording said "none are marked new yet", and then the
        # dashboard's New tab showed all of them, because the two count
        # different things: this line is per-RUN, and the tab is per-DATE.
        # Both are defensible on their own and the pair is a contradiction on
        # the one day a person has no idea which to believe. So this says what
        # the reader will actually see.
        _say(f"  {len(kept)} match your config. This is the first scan, so all "
             f"of them are new and the dashboard shows them that way; from the "
             f"next scan this line reports only what changed.")
    else:
        _say(f"  {len(kept)} match your config, {len(new_now)} new")
    if truncated and not args.dry_run:
        # Boards 26..307 were never asked. Their roles enter the database on
        # the next full scan and are stamped new then, which is not what new
        # means.
        #
        # Outside the else, because it used to sit inside it and a FIRST run
        # took the branch above -- so `job-radar scan --limit 200`, which is
        # the quick look the wizard recommends by name, never once said it had
        # read a fraction of the list to the one person who most needed to
        # know. The number is what was really read, not what was asked for.
        #
        # Not guarded on `kept` either, for the same reason one step further
        # on. `--limit` takes the head of the source list, which is one
        # platform's boards rather than a sample of the market, so a limited
        # scan can very easily match nothing. That run then printed "Nothing
        # matched. Where they went:" and blamed the titles, and the one fact
        # that explained it -- that 300 of 17,817 sources had been read --
        # was suppressed by the `and kept` that used to be on this line.
        # Zero matches is exactly when somebody needs to be told the list was
        # cut.
        _say(f"  (only {len(srcs):,} of your {all_srcs:,} sources were read; "
             f"roles on the rest will be marked new when a full scan first "
             f"sees them)")
    if not args.dry_run:
        # Collapse copies of the same job that arrived from different sources
        # on different runs, and repair links built with a path that 404s.
        # Both are about rows already stored, which scan-time dedupe cannot
        # reach.
        fixed = store.repair_smartrecruiters_urls(con)
        if fixed:
            _say(f"  repaired {fixed} broken apply link(s)")
        dupes = store.merge_duplicates(con, cfg)
        if dupes:
            _say(f"  merged {dupes} duplicate(s) into the employer's own listing")

    if not args.dry_run and not args.no_enrich:
        _enrich_step(con, cfg)

    if kept:
        _coverage_note(kept, srcs, cfg)
    _staleness_note(cfg)

    # A scan that could not read anything is not a scan that found nothing,
    # and until this existed the two were the same run from the outside: exit
    # 0, the working dashboard overwritten with an empty one, and the verdict
    # "Nothing matched. Most often this is the titles. Check
    # `titles.include`" -- which blames the reader's config for their wifi.
    # The proof was already on screen and unread: the "Where they went"
    # breakdown printed empty, because nothing had been screened at all.
    #
    # `ok` is how many sources answered. When that is none of them, say so
    # instead, leave `out/` alone rather than replacing a dashboard that
    # still has yesterday's roles on it, and exit non-zero so a cron job
    # notices.
    # Keyed on results rather than on `len(srcs)`, so "we asked everybody and
    # nobody answered" is distinguished from "nothing was asked". A caller
    # that hands back no results at all has not tried, and several tests stub
    # exactly that.
    blind = bool(results) and ok == 0
    if blind:
        _say("")
        _say(f"  None of your {len(results):,} sources answered. This is not "
             f"a scan that found nothing, it is a scan that could not look.")
        _say("  Check your connection first. Your existing dashboard and "
             "database are untouched.")
        if throttled:
            _say(f"  {len(throttled)} host(s) were slowing us down, which may "
                 f"be the whole story: {', '.join(sorted(throttled)[:4])}")
        con.close()
        return 2

    if not kept:
        # An empty page reads as "the market is empty" when it usually means
        # the filters or the sources do not fit the person running it.
        _say("")
        _say("  Nothing matched. Where they went:")
        ranked = sorted(dropped.items(), key=lambda x: -x[1])
        for reason, n in ranked[:5]:
            _say(f"    {n:>6}  {reason}")
        if len(ranked) > 5:
            _say(f"    {sum(n for _, n in ranked[5:]):>6}  in "
                 f"{len(ranked) - 5} smaller reasons")
        # `screen.run` dedupes before it starts counting reasons, so the
        # reasons add up to the post-dedupe total while the line above them
        # says "5,829 postings", which is pre-dedupe. The two numbers were
        # 891 apart on a 300-board run and nothing said why. A heading that
        # reads "Where they went" is a claim to account for all of them, and
        # an unexplained 15% gap under it is the reader's arithmetic going
        # wrong rather than ours.
        merged = len(all_jobs) - len(kept) - sum(dropped.values())
        if merged > 0:
            _say(f"    {merged:>6}  the same role posted more than once, "
                 f"merged")
        total_srcs = len(src_mod.load_file(src_mod.BUNDLED)) if cfg.use_bundled_sources else 0
        if cfg.sectors and total_srcs:
            _say(f"    your `sectors` setting cut the bundled list to {len(srcs)} "
                 f"of {total_srcs} sources")
        _say("")
        _say("  Most often this is the titles. Check `titles.include` in "
             f"{cfg.path} matches how postings are actually worded,")
        _say("  and add employers yourself with `job-radar discover <company>"
             " --add`.")

    outdir = Path(args.out or cfg.out_dir)
    written = []
    unwritable = ""
    # A dry run prints "nothing was recorded", which was true of the database
    # and false of the filesystem: it still overwrote out/index.html and
    # out/roles.json, so `--limit 200 --dry-run` replaced a full dashboard
    # with a 200-source sample of one.
    #
    # The whole block is guarded, even though `out_dir_problem` has already
    # said the directory is writable, because that was true when the command
    # was typed and this is an hour later: a disk fills up, a network share
    # drops, someone changes a mode. What must not happen is what used to --
    # a bare traceback out of `atomic_write_text` as the last act of a
    # 77-minute run, reading exactly like the scan itself was lost.
    # The board as it stands now: after the repair, the merge and the
    # enrichment, which is the state a reader opening the page will find in
    # the database behind it.
    board = kept if args.dry_run else store.live_jobs(con)
    new = [j for j in board if j.uid in new_ids]
    seen = [j for j in board if j.uid not in new_ids]

    # `meta` describes the same thing the payload beside it does.
    #
    # It carried the SCAN's counts while the page and roles.json carried the
    # BOARD's, so `roles.json` shipped `meta.matching = 24` above 1,064
    # entries. `postings` and `sources_ok` stay scan-scale and are labelled as
    # such by the page, which says "N postings across M boards": that is a
    # statement about the run, and it is the one number here a reader would
    # expect to be about the run.
    meta = {
        "sources_ok": ok, "sources_total": len(srcs),
        "postings": len(all_jobs), "matching": len(board),
        "new": len(new), "scanned_matching": len(kept),
        "scanned_new": len(new_now),
        "throttled": throttled, "dropped": dropped,
    }

    try:
        if args.dry_run:
            _say("  (dry run, so out/ was left alone)")
        elif "html" in cfg.formats:
            written.append(output.html_out.write(
                outdir / "index.html", new=new, seen=seen, dropped=dropped,
                sources_ok=ok, sources_total=len(srcs), throttled=throttled,
                postings=len(all_jobs),
            ))
        if not args.dry_run and "json" in cfg.formats:
            written.append(output.write_json(outdir / "roles.json", new, seen, meta))
        if not args.dry_run and ("markdown" in cfg.formats or "md" in cfg.formats):
            written.append(output.write_markdown(outdir / "roles.md", new, seen, meta))
    except OSError as exc:
        unwritable = str(exc)

    if not args.dry_run:
        # A one-directional export so a fresh GitHub Actions runner has a
        # seen-set to commit. Nothing reads it back except a clone with no
        # database yet.
        state.record(kept, counts)
        state.save()
        con.close()

    for p in written:
        _say(f"  wrote {p}")
    if unwritable:
        # Say where the roles ARE. Everything this scan found was committed to
        # the database several steps ago, so this is a rendering failure and
        # not a lost scan, and the difference is the difference between
        # shrugging and running the whole hour again.
        _say("")
        _say(f"  Could not write into {outdir}: {unwritable}")
        _say(f"  The {len(kept):,} role(s) this scan found are already in the "
             f"database and nothing was lost. `job-radar serve` reads them "
             f"straight from it,")
        _say(f"  and `job-radar list` prints them. Fix the permissions, or "
             f"point `output.dir` somewhere else, and the next scan writes "
             f"the files.")
        return 1
    return 0


# ---------------------------------------------------------------- discover
def cmd_discover(args) -> int:
    results = []
    for target in args.targets:
        _say(f"Looking for {target}...")
        found = run_discover(target, company=args.company, validate=not args.no_validate)
        if not found:
            # Telling someone to try the careers page URL when they just gave
            # you the careers page URL is a dead end with no next step in it.
            if target.startswith("http"):
                _say("  nothing found. Their careers page did not reveal a "
                     "job board this tool can read, and no board answered to "
                     "their name. Either it is rendered by JavaScript, or the "
                     "platform has no adapter yet. Look for the URL the "
                     "'View vacancies' link goes to and try that; if it is on "
                     "a platform listed under 'What this does not cover' in "
                     "the README, there is nothing to add.")
            else:
                _say("  nothing found. Try their careers page URL directly, "
                     "or the URL you land on after clicking through to their "
                     "vacancy list.")
            continue
        for f in found:
            if f.identity == "blocked":
                _say(f"  blocked. {f.note}")
                continue
            if f.identity == "unsupported":
                _say(f"  found their board: {f.note}")
                continue
            if f.identity == "unreadable":
                # The board exists; the fetch failed. Saying "nothing found,
                # either it is rendered by JavaScript or the platform has no
                # adapter yet" about a board we located and named is three
                # false statements at once. Counted as a result so the exit
                # code is a success, but never handed to `--add`: an unread
                # board is unverified, and writing it into the config would
                # bank a guess we could not check.
                _say(f"  {f.platform:<16}    ?  jobs  [could not read]  {f.url}")
                _say(f"                   found, but {f.note}")
                _say(f"                   not added; try again later")
                results.append(f)
                continue
            mark = {"ok": "verified", "mismatch": "WRONG COMPANY?",
                    "unchecked": "unverified"}.get(f.identity, f.identity)
            _say(f"  {f.platform:<16} {f.live_jobs:>4} jobs  [{mark}]  {f.url}")
            if f.note:
                _say(f"                   {f.note}")
            results.append(f)

    good = [f for f in results if f.live_jobs > 0
            and f.identity not in ("mismatch", "unreadable")]
    if args.add and good:
        # The write path, not the read path. `--add` edits a config, so it is
        # subject to the same rule as `setup`: it must never land on a file
        # the repo distributes. The two were split when setup was found
        # writing the tracked config.yaml on a fresh clone, and this caller
        # was left on the old one, which is the half of that bug fb6cc68
        # already failed to fix once.
        cfg_path = _cfg_write_path(args.config)
        if not cfg_path.exists():
            # Writing a file containing only a sources block produced a config
            # that then failed to load with "titles.include is empty", which
            # points at the wrong problem.
            _say(f"\nNo config at {cfg_path}. Run `job-radar setup` first, "
                 f"then re-run this with --add.")
            return 1
        n = _append_sources(cfg_path, [f.to_source() for f in good])
        if n:
            _say(f"\nAdded {n} source(s) to {cfg_path}")
        else:
            # It used to say "Added 1" while correctly writing nothing, so
            # running the same discover twice looked like it had duplicated
            # the entry.
            _say(f"\nAlready in {cfg_path}; nothing to add.")
    elif good and not args.add:
        _say("\nRe-run with --add to write these into your config.")
    return 0 if results else 1


# After this long without a check, the bundled list is worth refreshing. Chosen
# to be quiet: the upstream job runs weekly, so anything under a month means
# the person is simply a few merges behind and nothing is likely broken yet.
# Upstream revalidates and extends the list weekly, so eight days means a
# missed cycle. Past that you are not looking at a slightly older list, you
# are looking at one that has started losing boards as employers migrate.
STALE_AFTER_DAYS = 8


def _enrich_step(con, cfg) -> None:
    """Fetch the full posting for roles whose source gave only a headline.

    Part of the scan rather than a separate command you have to know about.
    It is a read, it spends no tokens, and without it a quarter of the board
    is unscreenable, unrankable and invisible to the salary floor: dealbreakers
    have no text to match, so they pass by default, which is the worst way for
    a filter to fail.
    """
    from . import enrich
    rows = enrich.candidates(con)
    if not rows:
        return
    _say(f"  fetching {len(rows)} postings that arrived as headlines only...")
    # Left on `enrich.run`'s own default of `fetch.DEFAULT_CONCURRENCY`, and
    # NOT wired to `cfg.concurrency`, which looks like the obvious tidy-up and
    # is a regression. Measured: this pass runs at 8.1 postings a second at 16
    # workers and 13.1 at 32, and the last full scan enriched 958 postings, so
    # the whole step is about two minutes of a fifty-minute run -- there is
    # nothing here worth chasing. Meanwhile plenty of configs still carry the
    # `concurrency: 4` the old advice recommended, and honouring that number
    # here would take those runs from 16 workers down to 4 and turn two
    # minutes into eight.
    got, tried = enrich.run(con, cfg, rows)
    if got:
        dropped = _rescreen(con, cfg)
        _say(f"  filled in {got} of {tried}; they can now be screened, ranked "
             f"and compared to your salary floor")
        if dropped:
            _say(f"  {dropped} of them failed a rule once their text was "
                 f"readable and have been hidden")
    else:
        _say(f"  none of the {tried} could be fetched. They stay as listings.")


def _rescreen(con, cfg) -> int:
    """Re-apply the filters to roles whose description has just arrived.

    Screening happens during the scan, against whatever text the source
    returned, which for LinkedIn, Workday and SmartRecruiters is nothing. So
    the filters ran on an empty string: a hard dealbreaker cannot match text
    that is not there, and a salary floor cannot compare a figure that was
    never parsed. Enrichment then fetched the text and nothing looked at it,
    which is the worst of both -- the tool had the sentence that disqualifies
    the role and showed the role anyway.
    """
    from . import screen as screen_mod, store
    from .models import Job, Salary
    import json as _json

    dropped = 0
    rows = con.execute(
        "SELECT r.* FROM roles r LEFT JOIN role_state s ON s.uid=r.uid "
        "WHERE COALESCE(s.status,'new')='new' "
        f"AND {store.LIVE_SQL} "
        "AND LENGTH(TRIM(COALESCE(r.description,'')))>=200").fetchall()
    for r in rows:
        job = Job(company=r["company"], title=r["title"], url=r["url"],
                  location=r["location"] or "", platform=r["platform"] or "",
                  description=r["description"] or "",
                  salary=Salary(min=r["salary_min"], max=r["salary_max"],
                                currency=r["salary_currency"],
                                period=r["salary_period"] or "year",
                                confirmed=bool(r["salary_confirmed"]),
                                raw=r["salary_label"]))
        keep, _hits = screen_mod.screen(job, cfg)
        if keep:
            keep, _why = screen_mod.apply_salary(job, cfg)
        if not keep:
            # Settle it rather than delete it: a role you were shown and then
            # told was wrong is worth being able to look back at, and deleting
            # it would make it "new" again on the next scan.
            store.set_status(con, r["uid"], "closed",
                             "hidden after its full description was read")
            dropped += 1
            continue
        # The old flags were written against an empty description and now
        # claim things that are no longer true.
        flags = [f for f in _json.loads(r["flags"] or "[]")
                 if "not screened" not in f]
        flags += [f for f in job.flags if f not in flags]
        con.execute("UPDATE roles SET flags=? WHERE uid=?",
                    (_json.dumps(flags), r["uid"]))
    return dropped


def _staleness_note(cfg) -> None:
    """Tell people their copy of the source list ages, and how to refresh it.

    Nothing said this anywhere. The weekly validation and growth jobs run
    upstream and open pull requests there; a clone freezes its list on the day
    it was cloned, and a fork only prunes its own, because the crawler that
    finds new employers deliberately does not ship in this repository. So a
    six-month-old checkout quietly loses boards as they migrate and never
    gains the ones that were added, while looking exactly as healthy as a
    fresh one.
    """
    if not cfg.use_bundled_sources:
        return
    days = src_mod.age_days()
    if days is None:
        return
    if days < STALE_AFTER_DAYS:
        return
    _say("")
    _say(f"  Your source list was last checked {days} days ago, and upstream "
         f"checks it weekly.")
    _say(f"  `git pull` gets you boards that have moved since and employers "
         f"added since. Without it this scan is quietly missing roles.")


def _daily_sync_nudge(cfg, db=None) -> None:
    """Say it once a day, on whatever command you happen to run.

    A warning attached only to `scan` reaches someone who scans. Someone who
    lives in `list` and `serve` never sees it, and their list is the one most
    likely to be old. Once a day is the honest frequency: often enough to
    matter within a week of a missed cycle, rare enough that it never becomes
    something to scroll past.
    """
    if not cfg.use_bundled_sources:
        return
    days = src_mod.age_days()
    if days is None or days < STALE_AFTER_DAYS:
        return
    from . import store
    from datetime import date
    # A nudge must not be the thing that creates the database. It runs before
    # every `list`, `serve` and `rank`, so with a mistyped `--db` it got there
    # first and left the stray file the command was then blamed for. No
    # database yet also means nowhere to remember "said it today", so on a
    # fresh install it is said every time until the first scan writes one --
    # which is a fortnight-old source list being mentioned to the one person
    # who has never scanned, and that is the right way round.
    if not Path(db or store.DEFAULT_PATH).is_file():
        _say(f"Your source list was last checked {days} days ago; upstream "
             f"checks it weekly. Run `git pull` to pick up boards that have "
             f"moved and employers added since.\n")
        return
    con = store.connect(db, must_exist=True)
    try:
        today = date.today().isoformat()
        if store.get_meta(con, "sync_nudge", "") == today:
            return
        store.set_meta(con, "sync_nudge", today)
    finally:
        con.close()
    _say(f"Your source list was last checked {days} days ago; upstream checks "
         f"it weekly. Run `git pull` to pick up boards that have moved and "
         f"employers added since.\n")


# Keyword searches return leads, not postings: no description, no salary, and
# often an agency rather than the employer.
_KEYWORD_PLATFORMS = {"linkedin", "nhs"}


def _coverage_note(kept, srcs, cfg) -> None:
    """Say what this result is actually made of.

    "80 match your config" was true and useless: zero of the 265 employer
    boards had produced a hospitality role, the top ten were NHS service
    managers, and no command in the tool would say so. The scan already knows
    every match's platform and sector, so this is arithmetic on numbers it has
    computed. Naming the composition lets someone see in one line that their
    field is not represented, instead of after two hours and a hand-written
    audit of sources.json.
    """
    from collections import Counter
    boards = [j for j in kept if j.platform not in _KEYWORD_PLATFORMS]
    leads = len(kept) - len(boards)

    if not boards:
        _say(f"  none from an employer board: all {len(kept)} are keyword "
             f"search leads, which carry no description and usually no "
             f"salary, so your dealbreakers and salary floor never ran.")
    elif leads:
        _say(f"  {len(boards)} from employer boards, {leads} keyword search "
             f"leads (no description, usually no salary).")

    sec = Counter((j.sector or "untagged") for j in kept)
    top = ", ".join(f"{k} {n}" for k, n in sec.most_common(4))
    _say(f"  by sector: {top}")
    _say("  If your field is not in that list, the bundled employers do not "
         "cover it. `job-radar discover <employer> --add` is the fix; adding "
         "twenty employers beats any setting in the config.")


def _cfg_path(raw) -> Path:
    """The config path as given, with surrounding whitespace removed.

    A path pasted with a stray leading space produced "no config at
    ` /path/c.yaml`", which reads as the file being missing rather than as a
    typo in the argument.
    """
    if raw:
        return Path(str(raw).strip()).expanduser().resolve()
    # config.local.yaml is the personal one and is gitignored; config.yaml
    # ships. Defaulting to the latter meant `discover <employer> --add` wrote
    # somebody's own board into the file the repo distributes, which is either
    # committed by accident or silently lost on the next pull.
    local = Path("config.local.yaml")
    return (local if local.exists() else Path("config.yaml")).resolve()


def _cfg_write_path(raw) -> Path:
    """Where `setup` and `discover --add` are allowed to write.

    Reading and writing want different answers. Reading should find whichever
    config exists. Writing must never land on a file the repo distributes: on
    a fresh clone neither personal config exists, so the read path fell
    through to `config.yaml`, and `job-radar setup` reported "Wrote
    config.yaml" while `git status` reported `M config.yaml`, 22 insertions
    and 43 deletions against a tracked file. Every later `git pull`
    conflicted, and on a public fork it is the user's own CV path that gets
    committed.

    Upstream no longer tracks `config.yaml`, so writing it on a fresh clone is
    now correct and creates an untracked file. That is deliberately still the
    default rather than `config.local.yaml`, because the GitHub Actions path
    documented in the README needs a config a runner can see, and a runner
    only sees what was committed.
    """
    return _cfg_path(raw)


def _cfg_or_default(raw) -> Config:
    """Load the config, or fall back to defaults only when none was asked for.

    Falling back silently when `-c` names a file that is not there meant a
    mistyped path produced a confident, complete, wrong answer: `coverage`
    reported the whole bundled list as though it were the user's own view.
    """
    p = _cfg_path(raw)
    if p.exists():
        return load_cfg(p)
    if raw:
        raise SystemExit(f"No config at {p}. Check the path, or run "
                         f"`job-radar setup -c {p}` to create it.")
    return Config()


def _append_sources(cfg_path: Path, new: list[Source]) -> int:   # used by discover --add
    """Append to `sources.extra` in place, keeping the file as written.

    Round-tripping through yaml.safe_dump rewrote the whole file and deleted
    every comment in it, including the one line that documents what
    `sources.extra` is -- so `--add` erased the explanation of `--add`.
    """
    import yaml
    text = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""
    raw = yaml.safe_load(text) or {}
    have = {s.get("url") for s in ((raw.get("sources") or {}).get("extra") or [])
            if isinstance(s, dict)}
    add = [s for s in new if s.url not in have]
    if not add:
        return 0

    block = "".join(
        f"    - company: {s.company}\n      url: {s.url}\n"
        f"      platform: {s.platform}\n" for s in add)

    lines = text.splitlines(keepends=True)
    out, done = [], False
    for i, line in enumerate(lines):
        # An existing empty `extra: []` becomes a list; an existing list is
        # appended to at the end of its block.
        if not done and re.match(r"^\s{2}extra:\s*\[\s*\]\s*$", line):
            out.append("  extra:\n"); out.append(block); done = True
            continue
        if not done and re.match(r"^\s{2}extra:\s*$", line):
            out.append(line)
            j = i + 1
            while j < len(lines) and (lines[j].startswith("    ") or not lines[j].strip()):
                # A lone `[]` under the key is an empty-list placeholder, not
                # a member of the list. Copying it through and then appending
                # entries put a sequence after a scalar and broke the file.
                if lines[j].strip() not in ("[]", ""):
                    out.append(lines[j])
                j += 1
            out.append(block); done = True
            lines[i + 1:j] = []
            continue
        out.append(line)
    if not done:
        out.append("\nsources:\n  extra:\n" + block)

    # Never hand back a file no command can load. `--add` is often the first
    # thing someone runs after `setup`, and a config broken here takes every
    # other command down with it.
    result = "".join(out)
    try:
        yaml.safe_load(result)
    except yaml.YAMLError as e:
        raise SystemExit(
            f"Refusing to write {cfg_path}: the result would not parse "
            f"({str(e).splitlines()[0]}). Add this by hand under "
            f"sources.extra:\n{block}")
    # Atomic, for the same reason as the check above: this rewrites a config
    # the user wrote by hand, and half of one is not recoverable from anything
    # this tool holds.
    atomic_write_text(cfg_path, result)
    return len(add)


# ---------------------------------------------------------------- validate
def cmd_seed_build(args) -> int:
    """Read the slow-phase boards and write a shard set. A maintainer's job.

    Deliberately its own command rather than a flag on `scan`. It screens
    nothing and stores nothing in anybody's database: a seed is a saved fetch,
    and the moment it carried a screening decision it would be carrying
    whoever built it's config into everybody else's search.
    """
    from . import seed as seed_mod
    from . import sources as src_mod
    from .screen import enrich as enrich_one
    cfg = load_cfg(args.config)
    # The BUNDLED list only, never the operator's own config.
    #
    # `sources.load(cfg)` adds `sources.extra` and applies `sectors` and
    # `sources.countries`. On this machine that meant a published seed would
    # have carried four boards nobody put on the bundled list -- Seamflow,
    # Balbix, Intel471, Dropzone -- which is not a list of employers, it is a
    # list of the companies the person building it has been applying to. A
    # public file is the wrong place to say that. And a `sectors` or
    # `countries` setting would have quietly published a seed narrowed to one
    # person's search while describing itself as the slow half of the scan.
    #
    # Read straight off the file for the same reason the seed carries no
    # score and no fit: what is published has to be the same for everybody,
    # whoever ran the command.
    srcs = [x for x in src_mod.load_file(src_mod.BUNDLED)
            if src_mod.phase_of(x) > 1]
    if args.limit:
        srcs = srcs[:args.limit]
    _say(f"Reading {len(srcs):,} slow-phase boards. This is the part a new "
         f"user should not have to wait for.")
    # Written as each board answers rather than collected and written at the
    # end. 267,000 adverts is about 1.7GB of text, and a build that runs out
    # of memory at minute seventy of a seventy-seven minute fetch has thrown
    # away the whole fetch.
    writer = seed_mod.Writer(args.out)
    done = {"n": 0, "roles": 0}

    def tick(res):
        done["n"] += 1
        if done["n"] % 250 == 0:
            _say(f"  {done['n']:,}/{len(srcs):,} boards, "
                 f"{done['roles']:,} roles")
        if res.ok:
            for j in adapters.parse(res.payload, res.source):
                # The board's own sector tag, the same line `cmd_scan.absorb`
                # runs. It was missing here, so every role in a shard was
                # untagged: a seeded dashboard showed one "Other" chip for the
                # lot, and `seed load` then wrote that emptiness over the
                # sectors a real scan had already stored.
                j.sector = j.sector or res.source.sector
                # Country and work mode are read here, not by the reader:
                # they are facts about the advert rather than answers to
                # anybody's config, and doing it once beats a quarter of a
                # million readers each doing it again.
                enrich_one(j)
                writer.add(j)
                done["roles"] += 1

    try:
        fetch_all(srcs, concurrency=cfg.concurrency, timeout=cfg.timeout,
                  retries=cfg.retries, user_agent=cfg.user_agent,
                  search_terms=cfg.titles_include,
                  blocks_path=Path(args.out) / "host-blocks.json",
                  api_keys={"reed": cfg.reed_api_key,
                            "adzuna_app_id": cfg.adzuna_app_id,
                            "adzuna_app_key": cfg.adzuna_app_key},
                  on_result=tick)
    except BaseException:
        # An hour of somebody else's bandwidth is not worth losing to a
        # Ctrl-C leaving fifty open file handles behind.
        writer.close()
        raise
    idx = writer.finish(boards=len(srcs))
    total = sum(v["bytes"] for v in idx["shards"].values())
    _say(f"\n{done['roles']:,} roles in {len(idx['shards'])} shards, "
         f"{total / 1e6:.0f}MB total, in {args.out}")
    for name, v in sorted(idx["shards"].items(),
                          key=lambda kv: -kv[1]["roles"])[:8]:
        _say(f"  {name:<10}{v['roles']:>8,} roles   {v['bytes'] / 1e6:>6.1f}MB")
    return 0


def cmd_seed_load(args) -> int:
    """Screen a shard set against THIS config and store what survives."""
    from . import seed as seed_mod, store
    cfg = load_cfg(args.config)
    src = args.path
    # The relocation countries too, and for the reason `sources.load`
    # already gives: `screen.match` allows `countries | relocate_to`, so a
    # config with `countries: [IN]` and `relocate_to: [SG, AE]` screens
    # Singapore roles happily and was never handed the Singapore shard to
    # screen. Measured on a 120-board seed: 284 roles read and 0 matches,
    # against 468 read and 6 matches once SG and AE are included -- and
    # the six were exactly the roles `relocate_to` exists to find.
    #
    # Computed HERE, above the download, because `fetch` was handed
    # `cfg.countries` alone while `describe` and `load` below were handed
    # this list. The download then fetched fewer shards than the read
    # demanded, and every config with a `relocate_to` in it died on
    # `the index lists a SG shard holding 20 roles, but SG.jsonl.gz is not
    # there. This shard set is incomplete or was written by a different
    # version of job-radar. Rebuild it rather than importing part of it.`
    # The published set was fine. The reader had simply never asked for that
    # shard, and the message sent them to rebuild somebody else's file.
    countries = list(dict.fromkeys(list(cfg.countries)
                                   + list(cfg.relocate_to)))
    if str(src).startswith(("http://", "https://")):
        # Kept, not thrown away. It is tens of megabytes and a second machine,
        # a second config or a re-import should not fetch it again.
        #
        # `.parent` is right for a config FILE and wrong for the `"."` that
        # stood in for one when `-c` was not given: `Path(".").resolve()` is
        # already the working directory, so its parent is one level ABOVE it.
        # `seed load <url>` with no `-c` therefore wrote into the parent of
        # wherever it was run, and docs/SEED.md promises `seed/` beside the
        # config. Two people working in sibling directories silently shared
        # one cache: /Users/cal/job-radar-seedtest/seed ended up holding PT,
        # UK and a 112MB US shard put there by three different runs, none of
        # which had asked for the others' countries.
        #
        # That is not merely untidy. `seed._wanted` reads an empty
        # `locations.countries` as "every shard on disk", so the next reader
        # with no countries set imports a neighbour's country out of a
        # directory neither of them named, and the run reports it as roles.
        keep = Path(args.keep) if args.keep else (
            Path(args.config).resolve().parent if args.config
            else Path.cwd()) / "seed"
        if args.dry_run:
            # Said before the download, not after it. A dry run genuinely has
            # to fetch the shards, because screening them means reading them,
            # and the file is kept and reused. What was wrong was the report:
            # it downloaded 134MB and then printed "Dry run, so nothing was
            # written", which is a sentence about the database being read as
            # one about the disk.
            _say("--dry-run writes nothing to the database, but it still has "
                 "to download the shards to screen them. They are kept and "
                 "reused, so this is not wasted.")
        try:
            seed_mod.fetch(src, countries, keep, say=_say)
        except (OSError, ValueError) as exc:
            _say(f"Could not download the seed from {src}: {exc}")
            # The one URL mistake worth naming, because GitHub answers it 200
            # with a web page rather than 404. `releases/tag/seed-latest` is
            # the page a person lands on from the repository and it differs
            # from the download base by two path segments, so `index.json`
            # under it is HTML and the run ended on
            # `Expecting value: line 8 column 1 (char 9)`, which describes a
            # JSON parser's disappointment and not the reader's mistake.
            if "/releases/tag/" in str(src):
                _say(f"That is the release's web page. The shard set lives "
                     f"under the download base: "
                     f"{str(src).replace('/releases/tag/', '/releases/download/')}")
            return 1
        _say(f"Kept in {keep}")
        src = keep
    try:
        idx = seed_mod.read_index(src)
    except FileNotFoundError:
        # "[Errno 2] No such file or directory: 'shards/index.json'" is true
        # and tells somebody who has never seen a shard set nothing at all.
        _say(f"No seed index at {src}. A shard set is a directory "
             f"holding index.json and one .jsonl.gz per country; "
             f"`job-radar seed build --out {src}` writes one.")
        return 1
    except (OSError, ValueError) as exc:
        _say(f"Could not read the seed at {src}: {exc}")
        return 1
    # Described from the shards that are ON DISK, not from every shard the
    # index names.
    #
    # `describe` reads an index, and an index describes the whole published
    # set. `load` reads a directory. Those are the same list only when the
    # whole set was downloaded, and for a reader with no countries configured
    # they are not: `seed.shards_for([])` is `unplaced, multiple`, so `fetch`
    # takes two shards, while `describe` and `seed._wanted` both read an empty
    # country list as "no country filter, take everything". The run printed
    #
    #     289,640 roles for AE, AR, ... US, 242MB, built 2026-08-28.
    #     21,337 roles read, 21,337 match your config.
    #     Stored.
    #
    # and exited 0. 93% of the seed was never fetched and nothing said so,
    # which is this project's own signature failure: a line that renders
    # exactly like a healthy import of the whole file.
    #
    # Filtering to what is here also names a shard left behind by an earlier
    # run under different countries, which `_wanted` will read when the
    # country list is empty and which the published index would have hidden
    # inside a total.
    root = Path(src)
    root = root if root.is_dir() else root.parent
    here = {k: v for k, v in (idx.get("shards") or {}).items()
            if (root / f"{k}.jsonl.gz").exists()}
    _say(seed_mod.describe({**idx, "shards": here}, countries))
    if not countries:
        # Said outright rather than left to be inferred from a shard list.
        # "No countries configured" is documented in `config.example.yaml` as
        # "no country filter", and a reader who set it that way is expecting
        # the lot.
        whole = idx.get("shards") or {}
        missing = sum(v.get("roles", 0) for k, v in whole.items()
                      if k not in here)
        if missing:
            _say(f"No countries are set in your config, so only the shards "
                 f"every reader gets were downloaded. {missing:,} more roles "
                 f"in {len(whole) - len(here)} shard(s) are published and "
                 f"were not fetched. Set `locations.countries` to the ones "
                 f"you want.")
    # Inside the same handling as `read_index` above, which it was not.
    # `seed.load` opens and decompresses the shards themselves, so it is the
    # call that meets a truncated download: a shard set whose index parses
    # fine and whose UK.jsonl.gz is eight bytes of HTML produced
    # `ValueError: UK.jsonl.gz is not a readable gzip file` under a nine-frame
    # traceback, one line after this command had printed a cheerful summary of
    # what it was about to read. `seed.load` writes an actionable sentence for
    # both of these now; the traceback above it was the only thing left.
    #
    # The screening is in here too, because `load` is a generator: nothing is
    # read off disk until something consumes it, and a `list()` on the line
    # above only looks like the place the file is touched.
    try:
        # Filtered on the title as each row is read, rather than building the
        # whole shard set in memory and screening afterwards. A 22,701-role
        # import held 325MB; a US reader's 151,044 would be about 2.1GB, and
        # the title gate throws away more than 99% of them.
        #
        # `screen_run` still sees every survivor and still dedupes across the
        # lot, so the answer is identical. It re-checks the title, which is
        # cheap and keeps this a pure optimisation rather than a second copy
        # of the rule.
        from .screen import title_gate
        gate = title_gate(cfg)
        read = 0
        jobs = []
        for j in seed_mod.load(src, countries):
            read += 1
            if gate(j.title):
                jobs.append(j)
        if not jobs:
            _say("Nothing in this index for your countries. Config `locations."
                 "countries`, or run a scan.")
            return 0
        # The pay is re-read from the advert before it is judged.
        #
        # A shard carries the figure whoever BUILT it parsed, on their version
        # of the code, and `docs/SEED.md` says a seed is a saved fetch and not
        # a saved decision. A parsed salary is a decision. `rescreen` was
        # taught this; `seed load` is the command the README puts first and
        # was not.
        #
        # Measured on one import: 23 of 131 roles carried pay after a load and
        # 55 after a rescreen with no new data, so 32 read "unconfirmed
        # salary" on the dashboard while the description beside them stated a
        # figure. Worse in the other direction: seven roles whose stated pay
        # is BELOW the floor were stored as having passed it.
        #
        # Only when the re-read is confirmed, same rule as `rescreen`: an
        # unconfirmed re-read means this parser found nothing in the text,
        # which is not evidence against a figure that came from a structured
        # field the advert never repeated.
        from .salary import currency_of_country, parse_text
        for j in jobs:
            # Only into a GAP. A re-read that finds a figure where the shard
            # carried none is a clear gain; one that REPLACES a confirmed
            # figure is a guess beating a fact.
            #
            # Found by a test whose fixture pads its adverts with
            # `hex(randomblob(200))`: 6 of 300 of those parse as a confirmed
            # salary, so the re-read overwrote a stored 20,000 with a number
            # out of random hex and the role then cleared a floor it fails.
            # It failed on CI and passed here, which is what a 2% chance
            # looks like.
            #
            # The cost is that a seeded figure parsed wrongly by an older
            # builder stays wrong until the weekly rebuild. That is a week of
            # one bad number against a chance of inventing one, and the
            # invented one is worse.
            if not j.salary.confirmed:
                fresh = parse_text(j.description or "",
                                   currency_of_country(j.country))
                if fresh.confirmed:
                    j.salary = fresh
        kept, _ = screen_run(jobs, cfg)
    except EOFError as exc:
        # A shard truncated ON DISK, which the index's byte check cannot see
        # because it only ever looks at what came down the wire. `seed.load`
        # guards its first `readline` against this and its row loop does not,
        # so a file cut off after row one raised EOFError out of the gzip
        # module, past `except (OSError, ValueError)` below, and reached the
        # user as a fifteen-frame traceback ending in "Compressed file ended
        # before the end-of-stream marker was reached". Caught here so the run
        # ends in a sentence; naming the shard belongs in `seed.load`, which
        # is the only thing that knows which file it was reading.
        _say(f"A shard in {src} is cut short ({exc}). The set is truncated or "
             f"was only partly written; re-fetch it, or delete the directory "
             f"and let `seed load <url>` download it again.")
        return 1
    except (OSError, ValueError) as exc:
        # One clause for both: a missing shard arrives as FileNotFoundError,
        # which is an OSError, and `seed.load` already names the shard and
        # says whether to rebuild or re-fetch. Splitting them only let this
        # command restate what the message underneath it had just said.
        _say(f"Could not read the seed at {src}: {exc}")
        return 1
    _say(f"{read:,} roles read, {len(kept):,} match your config.")
    if args.dry_run:
        _say(f"Dry run: nothing was written to the database"
             + (f". The shards are in {keep}." if str(args.path).startswith(
                 ("http://", "https://")) else "."))
        return 0
    con = store.connect(args.db)
    # The legacy import follows the database, not the working directory --
    # the same rule `cmd_scan` applies, and this call was left on the default.
    # `seed load <dir> --db /tmp/try.db` read this directory's state/seen.json
    # and applications.local.yaml and copied a real seen-set and application
    # history into the scratch file. Verified: a database asked for one seed
    # role came back holding that role plus every uid in the cwd's state file.
    # `--db` reads as isolation and was not one.
    own_db = not args.db or Path(args.db) == store.DEFAULT_PATH
    try:
        store.migrate(con,
                      state_path="state/seen.json" if own_db else "",
                      apps_path=None if own_db else "")
        # Stamped with the day the shard set was BUILT, not today. These
        # roles were first seen by whoever built it, often a week ago, and
        # calling them all new today made `list --new` answer with the whole
        # database: a scan said 3 new and `list --new` said 437, one minute
        # apart, against the same rows, at exactly the point in the README
        # where that sequence is recommended.
        store.upsert_roles(con, kept, first_seen=idx.get("generated") or None)
        con.commit()
    finally:
        con.close()
    # The real age, not "a day old at best".
    #
    # That sentence was true of a set fetched the morning it was published and
    # false of every other one, and this command's whole job is importing a
    # file somebody else built at a time the reader did not choose. The
    # published set is rebuilt weekly, and `seed load ./seed` on a directory
    # downloaded in June said "a day old at best" about roles four months
    # dead, one line under a `built 2026-06-01` the reader had no reason to
    # read as a warning. A stale import is not visibly different from a fresh
    # one: the roles are simply gone from the boards, which looks exactly like
    # a role nobody wants to fill.
    age = _seed_age_days(idx.get("generated"))
    if age is None:
        old = "This set does not say when it was built"
    elif age <= 1:
        old = "These are a day old at best"
    else:
        old = f"These were built {age:,} day{'s' if age != 1 else ''} ago"
    _say(f"Stored. {old} and the fast half of the sources is not in them at "
         f"all, so run a scan when you can: its answer wins on every field.")
    return 0


def _seed_age_days(generated) -> int | None:
    """How many days ago the shard set was built, or None if it will not say.

    None rather than 0. "We cannot read the build date" and "it was built
    today" are the two answers this repo keeps confusing, and the second one
    is the one that reassures.
    """
    from datetime import date as _date
    try:
        built = _date.fromisoformat(str(generated))
    except (TypeError, ValueError):
        return None
    return max(0, (_date.today() - built).days)


def cmd_validate(args) -> int:
    cfg = _cfg_or_default(args.config)
    srcs = src_mod.load_file(args.file) if args.file else _load_sources(cfg)
    if args.limit:
        srcs = srcs[: args.limit]
    _say(f"Validating {len(srcs)} sources...")

    rows, dead, mismatch, unread = [], [], [], []
    from concurrent.futures import ThreadPoolExecutor

    # Paced per host, and the six-worker cap is gone with it. The cap was the
    # only brake this command had, and it was the wrong one: it slowed the
    # ~7,777 hosts holding a single board each without doing anything about the
    # four thousand consecutive Greenhouse boards. Unpaced, a burst here reads
    # as a dead board, and `--prune` then deletes a live employer.
    limiter = HostLimiter()

    def paced(src):
        pace_this_thread(limiter)
        # A URL with a placeholder nothing can fill in raises inside
        # `validate_source`, and that exception comes back out of `ex.map`
        # below and ends the run: one odd source in `sources.extra` meant the
        # thousands after it were never checked and nothing said so. Report it
        # as its own row instead. "unreachable" is the honest verdict -- it
        # was never read -- and unreachable rows are never pruned, which is
        # right, because a URL this tool cannot build is not evidence that the
        # employer stopped hiring.
        bad = src_mod.url_template_error(src)
        if bad:
            return {"company": src.company, "url": src.url,
                    "platform": src.platform, "live_jobs": 0,
                    "verdict": "unreachable", "transport": None,
                    "prunable": False,
                    "note": f"could not be read: {bad}"}
        return validate_source(src)

    with ThreadPoolExecutor(max_workers=max(1, cfg.concurrency)) as ex:
        for i, row in enumerate(ex.map(paced, interleave_by_host(srcs)), 1):
            rows.append(row)
            if row["verdict"] == "dead":
                dead.append(row)
            elif row["verdict"] == "unreachable":
                unread.append(row)
            elif row["verdict"] == "mismatch":
                mismatch.append(row)
            if i % 25 == 0:
                _say(f"  {i}/{len(srcs)}")

    _say(f"\n  live: {len(rows) - len(dead) - len(unread)}   "
         f"dead: {len(dead)}   unreachable: {len(unread)}   "
         f"identity mismatch: {len(mismatch)}")
    for r in dead[:40]:
        _say(f"  DEAD      {r['company']} <- {r['url']}")
    for r in unread[:20]:
        _say(f"  UNREAD    {r['company']}: {r['note']}")
    if unread:
        _say(f"  {len(unread)} could not be read and are left alone. They are "
             f"not dead; try again later.")
    for r in mismatch[:40]:
        _say(f"  MISMATCH  {r['company']}: {r['note']}")

    if args.report:
        # Atomic. This is the only durable trace of a validation run that took
        # hours of network, and it is what `--prune` is argued from. Killed
        # part way through, the run's own results are gone either way, but the
        # previous report is not, and half a JSON file is not a report.
        atomic_write_text(Path(args.report), json.dumps({
            "checked": datetime.now().isoformat(timespec="seconds"),
            "total": len(rows), "dead": dead, "mismatch": mismatch, "rows": rows,
        }, indent=1))
        _say(f"  wrote {args.report}")

    if args.prune and not args.file:
        _say("  --prune needs --file: it rewrites a source list, and there is "
             "no file to rewrite without one.")
    if args.prune and args.file:
        # A prune has to be able to tell "these boards are gone" from "this
        # machine has no network". A failed fetch is now its own verdict and
        # is never pruned, which is the real fix. This threshold stays as the
        # second line: if a platform starts answering 200 with an empty array
        # instead of an error, its whole tenancy looks dead at once and no
        # per-request check would catch it. This runs unattended every Sunday
        # in Actions, on runners the README itself says get throttled sooner
        # than a laptop.
        share = len(dead) / max(1, len(rows))
        if share > 0.25 and len(dead) > 5:
            _say(f"\n  REFUSING TO PRUNE: {len(dead)} of {len(rows)} sources "
                 f"({share:.0%}) came back empty.")
            _say("  That is a platform or rate-limit problem, not that many "
                 "boards dying at once. Nothing was changed.")
            _say("  Re-run when the connection is good, or use --force-prune "
                 "if the list really has collapsed.")
            if not args.force_prune:
                return 1
        # Deleted on the row's own `prunable` flag, not on its verdict.
        # `discover` sets that flag, and it is stricter than "the verdict says
        # dead": a row that never reached HTTP, or that came back with a TLS
        # alert, can never be deletable however the verdict reads. Arguing
        # from the verdict here happened to agree with the flag today, which
        # is not the same as being right, and the day they disagree the cost
        # is a live employer deleted from the shipped list.
        prunable_rows = [r for r in rows if row_prunable(r)]
        held = len([r for r in dead if not row_prunable(r)])
        if held:
            _say(f"  {held} source(s) read as dead but are not deletable "
                 f"(nothing reached the board), so they are kept.")
        dead_urls = {r["url"] for r in prunable_rows}
        keep = [s for s in srcs if s.url not in dead_urls]
        src_mod.save(keep, args.file, meta={"pruned": len(srcs) - len(keep),
                                            "checked": datetime.now().date().isoformat()})
        _say(f"  pruned {len(srcs) - len(keep)} dead sources from {args.file}")
    return 0


# ---------------------------------------------------------------- coverage
def cmd_coverage(args) -> int:
    cfg = _cfg_or_default(args.config)
    srcs = src_mod.load_file(args.file) if args.file else _load_sources(cfg)
    cov = src_mod.coverage(srcs)
    _say(f"{cov['total']} sources\n")
    for label, key in (("By sector", "by_sector"), ("By country", "by_country"),
                       ("By platform", "by_platform")):
        _say(label)
        for k, v in cov[key].items():
            _say(f"  {v:>5}  {k}")
        _say()

    # The command is documented as "where the list is thin", but it used to
    # be a static dump that never looked at the config. A hospitality manager
    # ran it, saw a healthy-looking 307, and had no way to learn there were
    # zero hospitality employers on it.
    if not args.file:
        if cfg.sectors:
            bundled = len(src_mod.load_file(src_mod.BUNDLED)) if cfg.use_bundled_sources else 0
            if bundled:
                _say(f"Your `sectors` setting narrows the bundled list to "
                     f"{cov['total']} of {bundled} sources.")
        # Derived from the list rather than named here. The hardcoded pair
        # missed `workable_search` the day it was added, so the line
        # undercounted and the reader was told fewer of their sources were
        # keyword searches than actually were. Same failure as `meta.boards`,
        # which drifted for exactly the same reason.
        kw_platforms = {s.platform for s in srcs if s.keyword_template}
        keyword = sum(n for k, n in cov["by_platform"].items()
                      if k in kw_platforms)
        if keyword:
            _say(f"{keyword} of these are keyword searches rather than "
                 f"employer boards: they return leads with no description "
                 f"and usually no salary, and they include agencies.")
        # `multi` counted alongside `untagged`, not left out of the sentence.
        # A board tagged `multi` is a multinational and is kept whatever
        # `sources.countries` says, which changed today; the message still
        # named only the untagged ones and so undercounted by 1,597 across
        # the bundled list, telling the reader their filter was tighter than
        # it is.
        always = sum(cov["by_country"].get(k, 0)
                     for k in ("untagged", "multi", "unknown"))
        if cfg.source_countries and always:
            _say(f"`sources.countries` only removes sources tagged for "
                 f"somewhere else. {always} here are untagged or "
                 f"multinational, and are always fetched.")
        _say("Nothing in your field? `job-radar discover <employer> --add` "
             "adds their board. Adding twenty employers does more for you "
             "than any setting in the config.")
    return 0


# ---------------------------------------------------------------- applied
def _resolve_uid(con, target: str):
    """Find one role from a URL, a company name, or a uid. Returns (uid, why).

    Fails loudly with the candidates rather than guessing: recording a status
    against the wrong role is worse than not recording it.
    """
    import re as _re
    t = target.strip()
    row = con.execute("SELECT uid FROM roles WHERE uid=?", (t,)).fetchone()
    if row:
        return row["uid"], ""

    # `job-radar list` prints a shortened uid, so accept what it printed.
    # Copying the visible id and being told "could not identify a role" is a
    # dead end with no next step in it.
    if _re.fullmatch(r"[0-9a-f]{6,15}", t):
        rows = con.execute("SELECT uid, company, title FROM roles "
                           "WHERE uid LIKE ?", (t + "%",)).fetchall()
        if len(rows) == 1:
            return rows[0]["uid"], ""
        if len(rows) > 1:
            return None, f"{len(rows)} roles start with {t!r}; give more of it"

    if t.startswith("http"):
        clean = _re.sub(r"[?#].*$", "", t.rstrip("/"))
        rows = con.execute(
            "SELECT uid, company, title FROM roles "
            "WHERE url=? OR url LIKE ?", (t, clean + "%")).fetchall()
        if len(rows) == 1:
            return rows[0]["uid"], ""
        if not rows:
            return None, f"no role in the database has that URL"
        return None, f"{len(rows)} roles share that URL"

    rows = con.execute(
        "SELECT uid, company, title FROM roles WHERE company LIKE ? "
        "ORDER BY last_seen DESC", (f"%{t}%",)).fetchall()
    if not rows:
        return None, f"nothing matches {t!r}"
    if len(rows) == 1:
        return rows[0]["uid"], ""
    listing = "\n".join(f"    {r['uid']}  {r['company']} - {r['title'][:52]}"
                         for r in rows[:8])
    return None, (f"{len(rows)} roles match {t!r}. Pick one by uid:\n{listing}")


def cmd_applied(args) -> int:
    """Record what happened with a role. Writes the database, same as the
    dashboard does, so the two cannot disagree."""
    from . import store
    con = store.connect(args.db, must_exist=True)
    try:
        if args.status not in store.STATUSES:
            _say(f"status must be one of: {', '.join(store.STATUSES)}")
            return 1
        uid, why = _resolve_uid(con, args.target)
        if not uid:
            _say(f"Could not identify a role: {why}")
            return 1
        row = con.execute("SELECT company, title FROM roles WHERE uid=?",
                          (uid,)).fetchone()
        store.set_status(con, uid, args.status, args.note)
        _say(f"{row['company']} - {row['title'][:56]}")
        _say(f"  -> {args.status}")
        return 0
    finally:
        con.close()


# ---------------------------------------------------------------- generate
def cmd_generate(args) -> int:
    """Run a screen, CV or cover letter from the command line.

    The same queue and the same runner the dashboard uses. The design's rule
    is that nothing generates on a schedule; an explicit command is still a
    deliberate spend.
    """
    from . import runner, store
    con = store.connect(args.db, must_exist=True)
    try:
        if args.kind not in runner.KINDS:
            _say(f"kind must be one of: {', '.join(runner.KINDS)}")
            return 1
        # Before the queue row and before "this spends tokens". Without the
        # CLI the row was still created and instantly marked failed, so the
        # jobs table filled with attempts that were never possible and the
        # user read a cost warning for a run that could not start.
        if not runner.claude_bin():
            _say(runner._no_claude_msg())
            return 1
        uid, why = _resolve_uid(con, args.target)
        if not uid:
            _say(f"Could not identify a role: {why}")
            return 1
        if args.kind == "cover_letter" and not store.has_artifact(con, uid, "cv"):
            _say("Draft the CV first: the letter is checked against it for "
                 "repeated phrasing.")
            return 1
        row = con.execute("SELECT company, title, description FROM roles "
                          "WHERE uid=?", (uid,)).fetchone()
        # Screening a posting with no body spends money to be told there is
        # nothing to read. For some users that is most of their results.
        if args.kind == "screen" and len((row["description"] or "").strip()) < 200:
            _say(f"{row['company']} - {row['title'][:56]}")
            _say("  This posting has no description, so there is nothing to "
                 "screen against your dealbreakers.")
            _say("  Open the advert and screen it by hand, or use --force to "
                 "spend the tokens anyway.")
            if not args.force:
                return 1
        job_id = store.enqueue(con, uid, args.kind)
        _say(f"{row['company']} - {row['title'][:56]}")
        _say(f"  {args.kind}, job {job_id}. This spends tokens.")
    finally:
        con.close()

    # Without config_path this resolves a config from the working directory,
    # so a run with -c pointed elsewhere gets screened against whatever
    # config.yaml happens to be next to it. That is how a nurse's role came
    # back screened against the author's job titles.
    runner.run_job(job_id, db_path=args.db, base=args.docs,
                   config_path=args.config)

    con = store.connect(args.db)
    try:
        j = con.execute("SELECT state, error FROM jobs WHERE id=?",
                        (job_id,)).fetchone()
        if j["state"] != "done":
            _say(f"  failed: {j['error']}")
            return 1
        for a in store.artifacts_for(con, uid):
            if a["kind"] == args.kind:
                rating = f"  {a['rating']:.0f}/100" if a["rating"] else ""
                if a["summary"]:
                    _say(f"  {a['summary']}")
                _say(f"  wrote {a['path']}{rating}")
                gates = json.loads(a["gates"] or "{}")
                # A screen has no gates to run. Printing "all passed" against
                # an empty dict read as a clean bill of health on a posting
                # that had not been read at all.
                if gates:
                    bad = [k for k, v in gates.items() if v is False]
                    _say(f"  gates: {'all passed' if not bad else 'FAILED ' + ', '.join(bad)}")
                break
        return 0
    finally:
        con.close()


# ---------------------------------------------------------------- enrich
def cmd_enrich(args) -> int:
    """Fill in descriptions for roles whose source only returned a headline."""
    from . import enrich, store
    cfg = _cfg_or_default(args.config)
    con = store.connect(args.db, must_exist=True)
    try:
        rows = enrich.candidates(con, limit=args.limit)
        if not rows:
            _say("Nothing to fetch. Every role on the board already has its "
                 "description.")
            return 0
        serial = args.pause is not None
        workers = 1 if serial else (args.concurrency
                                    or fetch_defaults.DEFAULT_CONCURRENCY)
        how = (f"one at a time with a {args.pause}s pause" if serial
               else f"{workers} at a time, each host paced separately")
        _say(f"{len(rows)} roles to fetch, {how}. No tokens are spent.")
        if args.dry_run:
            return 0

        def progress(i, total, got):
            if i % 10 == 0 or i == total:
                _say(f"  {i}/{total}, {got} filled in")

        got, tried = enrich.run(con, cfg, rows, pause=args.pause or 0.0,
                                on_each=progress, concurrency=workers)
        _say(f"\nFilled in {got} of {tried}.")
        if got:
            _say("They can now be screened, ranked and compared to your "
                 "salary floor. `job-radar rank` picks them up.")
        return 0
    finally:
        con.close()


# ---------------------------------------------------------------- rank
def cmd_rank(args) -> int:
    """Score every role against the CV in one batched pass.

    Deliberately a command rather than something the scan does. It spends
    tokens, and the rule everywhere else in this tool is that nothing is spent
    without being asked for. It says what it will cost before it starts.
    """
    from . import rank as rank_mod, store
    cfg = _cfg_or_default(args.config)
    con = store.connect(args.db, must_exist=True)
    try:
        rows = rank_mod.candidates(con, refresh=args.refresh)
        if not rows:
            _say("Nothing to rank. Every role with a description already has a "
                 "fit score; use --refresh to score them again.")
            return 0
        if args.limit:
            rows = rows[: args.limit]
        batches, tokens = rank_mod.estimate(rows)
        _say(f"{len(rows)} roles to rank, in {batches} call(s), roughly "
             f"{tokens:,} input tokens.")
        _say(f"Screening these one at a time would be about "
             f"{len(rows) * rank_mod.SCREEN_TOKENS:,}.")
        if args.dry_run:
            _say("dry run: nothing sent")
            return 0
        # After the dry-run branch, so estimating a spend still works on a
        # machine that could not make it, and before the real one, so the
        # missing-CLI message does not land underneath a cost estimate for a
        # run that was never going to start.
        from . import runner
        if not runner.claude_bin():
            _say(runner._no_claude_msg())
            return 1

        def progress(done, total, scored):
            _say(f"  {done}/{total} sent, {scored} scored")

        n = rank_mod.rank(con, cfg, rows, on_batch=progress)
        _say(f"\nScored {n} of {len(rows)}.")
        if n < len(rows):
            _say(f"  {len(rows) - n} came back unscored and keep fit -1; they "
                 f"are not ranked as bad, they are unranked. Run again to "
                 f"retry just those.")
        top = con.execute(
            "SELECT company,title,fit,fit_why FROM roles WHERE fit>=0 "
            "ORDER BY fit DESC, score DESC LIMIT ?", (args.top,)).fetchall()
        if top:
            _say("")
            for r in top:
                _say(f"  {r['fit']:>3}  {r['company'][:24]:<26} {r['title'][:44]}")
                if r["fit_why"]:
                    _say(f"       {r['fit_why'][:104]}")
        return 0
    finally:
        con.close()


# ------------------------------------------------------------ rescreen
def cmd_rescreen(args) -> int:
    """Re-apply the current config to roles already in the database.

    A scan filters what it fetched that day and never looks back, so every
    change to titles, locations, dealbreakers or the salary floor applies only
    to roles found afterwards. Tighten an exclude and the roles it was written
    for stay on the dashboard for ever; widen an include and nothing already
    stored is reconsidered. Measured on a real database after a day of config
    changes: 196 of 1,670 roles, 11.7%, no longer matched the config that was
    supposedly producing them.

    Reporting is the default and removal needs `--remove`, because this is the
    one command whose whole job is to delete rows somebody may have been
    relying on. A role you have touched is never removed whatever it matches:
    the status is a decision you made and outranks a filter.
    """
    from . import store
    from .models import Job, Salary
    from .screen import (match, apply_salary, screen as screen_one,
                         enrich as enrich_derived)

    cfg = _cfg_or_default(args.config)
    con = store.connect(args.db, must_exist=True)
    try:
        # The salary columns and the real URL are selected because this has to
        # run the SAME filters a scan runs. It used to call `match` alone,
        # which is the title and location gate and nothing else, while the
        # sentence above this promised dealbreakers and the salary floor as
        # well. Add a hard dealbreaker matching every stored role, or raise
        # the floor past every stated figure, and this command answered "All
        # N roles still match your config" -- a wrong number that reads
        # exactly like a right one, on the command whose only job is to be
        # the second opinion.
        rows = con.execute(
            "SELECT r.uid, r.company, r.title, r.url, r.platform, r.location, "
            "r.description, r.salary_min, r.salary_max, r.salary_currency, "
            "r.salary_period, r.salary_confirmed, r.salary_label, "
            "r.city, r.country, r.work_mode, r.employment, "
            "COALESCE(s.status,'new') st "
            "FROM roles r LEFT JOIN role_state s ON s.uid=r.uid").fetchall()
        stale, kept_by_status = [], []
        rederived = 0
        for r in rows:
            j = Job(company=r["company"], title=r["title"],
                    url=r["url"] or "https://example.invalid/x",
                    platform=r["platform"],
                    location=r["location"] or "", description=r["description"] or "",
                    salary=Salary(min=r["salary_min"], max=r["salary_max"],
                                  currency=r["salary_currency"],
                                  period=r["salary_period"] or "year",
                                  confirmed=bool(r["salary_confirmed"]),
                                  raw=r["salary_label"]),
                    # Carried in, not left at the default.
                    #
                    # `employment` can come from the PLATFORM's own field,
                    # which six of them supply and which is not stored
                    # anywhere except in this column. Building the Job with
                    # the default "unstated" and re-deriving threw that away
                    # and replaced it with a guess at the prose: a Workable
                    # posting typed "Contract" by its employer, whose text
                    # says nothing, would be silently downgraded to
                    # "unstated" by the very command meant to refresh it.
                    # Only a fresh scan can recover it, so nothing here may
                    # discard it.
                    employment=r["employment"] or "unstated")
            # Re-derive the stored columns, not only the verdict.
            #
            # `city`, `country` and `work_mode` are computed by `screen.enrich`
            # at scan time and then written into the table, so every fix to
            # that derivation, or to an adapter feeding it, reached new rows
            # only. This command re-ran the titles, the locations, the
            # dealbreakers and the floor, reported honestly on all four, and
            # left those three columns holding whatever the scan that first
            # saw the role happened to think -- which is the dashboard's city
            # filter, its work-mode filter and the right-to-work gate all
            # reading a stale answer with nothing anywhere saying so. Measured
            # on one row: a Manchester posting stored as city "US Remote",
            # country US, mode remote stayed exactly that through a rescreen
            # that printed "All 1 roles still match your config."
            #
            # Run BEFORE `match` here, where `screen.run` runs it after. That
            # is deliberate and it is not the ordering that file calls
            # load-bearing: `match` reads nothing `enrich` sets (it resolves
            # countries itself), and `enrich` still lands before `apply_salary`
            # and `screen`, which is the part that matters. `screen.run` puts
            # it second purely to skip the 85% of screening CPU it costs on
            # the 99% of postings the title gate throws away. There is no such
            # saving here: a role that fails the gate may still be kept
            # because you acted on it, and it would then be the one row on the
            # dashboard nobody had refreshed.
            enrich_derived(j)
            city, mode = j.city or "", j.work_mode or "unstated"
            # Permanent, contract or unstated, for the same reason as the
            # three above: `employment` is derived at scan time and written
            # into the table, so every improvement to the classifier reaches
            # only rows found afterwards. This is the command that backfills
            # it, and on the database it was written against it was the only
            # thing that could: 5,474 rows had been stored before the column
            # existed and every one of them read "unstated".
            #
            # A downgrade to "unstated" is allowed here, unlike in
            # `store.upsert_roles`, and the difference is what the two are
            # looking at. A scan pass may hold no description at all, so its
            # "unstated" means "I could not see"; this reads the same stored
            # text that produced the old value, so with a description present
            # its "unstated" means "I looked, and the advert does not say".
            # Without one it is back to not being able to see, and the stored
            # answer stands.
            emp = j.employment or "unstated"
            was_emp = r["employment"] or "unstated"
            if emp == "unstated" and not (r["description"] or "").strip():
                emp = was_emp

            # The salary too, for the same reason and it was the one derived
            # column left out. It is read from the advert by a parser that
            # changes, and the stored figure is whatever the code understood
            # on the day the row was written. Two ways that goes wrong and
            # neither had any command that could reach it:
            #
            #   * 15 of 236 roles on one board read "unconfirmed salary" on
            #     the dashboard while the description in the same database
            #     plainly stated a range, because the parser has since learnt
            #     to read it.
            #   * A seeded row carries a figure computed by whoever BUILT the
            #     shard set, on their version. `docs/SEED.md` says a seed is a
            #     saved fetch and not a saved decision, and a parsed salary is
            #     a decision of the code version. On a set built before this
            #     morning's lakh fix, "Rs 16,50,000" is stored, confirmed, as
            #     78,000.
            #
            # The new reading wins only when it is confirmed. An unconfirmed
            # re-read means this parser found nothing in the text, which is
            # not evidence against a figure that came from a structured field
            # the description never repeated.
            # Only into a gap, for the reason `cmd_seed_load` gives at
            # length: a re-read that replaces a confirmed figure is a guess
            # beating a fact, and text that is not pay parses as pay often
            # enough to matter.
            from .salary import currency_of_country, parse_text
            if not j.salary.confirmed:
                fresh = parse_text(j.description or "",
                                   currency_of_country(r["country"]))
                if fresh.confirmed:
                    j.salary = fresh
            # The board's own country tag is the fallback a scan uses when the
            # posting names nowhere, and it is not stored per role, so this
            # command cannot recompute it. Where the location names no country
            # the stored value is kept rather than blanked: an empty country
            # is read by the country filter as "not here", and dropping a role
            # out of somebody's results is a worse error than an old tag.
            # `city` and `work_mode` have no such problem -- both are computed
            # from the location and the description, both of which are stored,
            # and "unstated" is an honest bucket the dashboard already has.
            # The one thing lost is a work mode that rested only on the
            # platform's own remote flag, which is not a column here; the next
            # scan restores it, and until then "unstated" is true where
            # "remote" was a claim nothing stored can still support.
            country = j.country or r["country"] or ""
            sal, was = j.salary, (r["salary_min"], r["salary_max"],
                                  r["salary_currency"],
                                  bool(r["salary_confirmed"]))
            now = (sal.min, sal.max, sal.currency, sal.confirmed)
            if (city, country, mode, emp) != (r["city"] or "", r["country"] or "",
                                              r["work_mode"] or "unstated",
                                              was_emp) \
                    or now != was:
                con.execute(
                    "UPDATE roles SET city=?, country=?, work_mode=?, "
                    "employment=?, "
                    "salary_min=?, salary_max=?, salary_currency=?, "
                    "salary_period=?, salary_confirmed=?, salary_label=? "
                    "WHERE uid=?",
                    (city, country, mode, emp, sal.min, sal.max, sal.currency,
                     sal.period or "year", 1 if sal.confirmed else 0,
                     sal.label(), r["uid"]))
                rederived += 1
            ok, _ = match(j, cfg)
            if ok:
                ok, _ = apply_salary(j, cfg)
            if ok:
                # A posting with no description cannot fail a dealbreaker, and
                # `screen` keeps it for exactly that reason. Nothing is removed
                # on the strength of text nobody ever fetched.
                ok, _ = screen_one(j, cfg)
            if ok:
                continue
            (kept_by_status if r["st"] not in ("new", "") else stale).append(r)

        con.commit()
        if rederived:
            # Said whatever the verdict is, and said first, because it is the
            # only thing this command CHANGES on a database where every role
            # still matches. "All 1,670 roles still match your config" and a
            # silent rewrite of three columns is a report that omits its own
            # only effect.
            _say(f"Re-derived the city, country, work mode, employment type "
                 f"or pay on {rederived} of {len(rows)} roles from the current "
                 f"rules.")
        if not stale and not kept_by_status:
            _say(f"All {len(rows)} roles still match your config.")
            return 0
        _say(f"{len(stale) + len(kept_by_status)} of {len(rows)} roles no longer "
             f"match your config.")
        if kept_by_status:
            _say(f"  {len(kept_by_status)} of them you have already acted on, so "
                 f"they stay whatever happens.")
        for r in stale[: args.limit or 15]:
            _say(f"    {r['company'][:24]:<25} {r['title'][:52]}")
        if len(stale) > (args.limit or 15):
            _say(f"    ... and {len(stale) - (args.limit or 15)} more")

        if not args.remove:
            _say(f"\n  Nothing was removed. `job-radar rescreen --remove` deletes "
                 f"the {len(stale)} untouched ones.")
            return 0
        for r in stale:
            con.execute("DELETE FROM role_state WHERE uid=?", (r["uid"],))
            con.execute("DELETE FROM roles WHERE uid=?", (r["uid"],))
        con.commit()
        _say(f"\n  Removed {len(stale)}. Kept {len(kept_by_status)} you had acted on.")
        return 0
    finally:
        con.close()


# ---------------------------------------------------------------- list
def cmd_list(args) -> int:
    """Everything the dashboard shows, as text."""
    from . import store
    # `must_exist`, because this is the command the bug was found on:
    # `job-radar list --db typo.db` created the file, printed `0 role(s)` and
    # exited 0. Every read command here now refuses to invent a database, for
    # the reason spelled out in `store.connect`. The two writers, `scan` and
    # `seed load`, still create one, because that is their job.
    con = store.connect(args.db, must_exist=True)
    try:
        q = ("SELECT r.*, COALESCE(s.status,'new') status, "
             "COALESCE(s.note,'') note FROM roles r "
             "LEFT JOIN role_state s ON s.uid=r.uid")
        params = []
        if args.status and args.status not in store.STATUSES:
            _say(f"status must be one of: {', '.join(store.STATUSES)}")
            return 1
        where = []
        if args.status:
            where.append("COALESCE(s.status,'new')=?")
            params.append(args.status)
        elif not args.all:
            where.append("COALESCE(s.status,'new') NOT IN "
                         "('rejected','withdrawn','skipped','closed')")
        if args.new:
            # Roles first seen on the most recent scan date. This is the
            # question a daily user actually has, and until now the only
            # answer was a count printed by `scan` that scrolled away.
            where.append(store.NEW_SQL)
        elif not args.all:
            # Same window the dashboard uses, so the two views agree. Without
            # it `list` accumulated every role ever seen and reported roles
            # that had not been on a board for weeks.
            where.append(store.LIVE_SQL + " OR COALESCE(s.status,'new') <> 'new'"
                         " OR r.uid IN (SELECT DISTINCT uid FROM artifacts)")
        # A role with no link is seen-set history, not a listing. See
        # store.ACTIONABLE_SQL.
        where.append(store.ACTIONABLE_SQL)
        if where:
            q += " WHERE " + " AND ".join(f"({w})" for w in where)
        q += " ORDER BY r.score DESC, r.company COLLATE NOCASE"
        rows = con.execute(q, params).fetchall()
        if args.limit:
            rows = rows[: args.limit]

        arts = {}
        for a in con.execute("SELECT * FROM artifacts"):
            arts.setdefault(a["uid"], []).append(a)

        if args.json:
            out = []
            for r in rows:
                d = dict(r)
                # These are TEXT columns holding JSON. Passing them straight
                # into json.dumps double-encoded them, so every consumer had
                # to parse a string inside the parsed document.
                for k in ("reasons", "flags"):
                    try:
                        d[k] = json.loads(d.get(k) or "[]")
                    except (json.JSONDecodeError, TypeError):
                        d[k] = []
                d["artifacts"] = [
                    {"kind": a["kind"], "path": a["path"], "rating": a["rating"],
                     "gates": json.loads(a["gates"] or "{}")}
                    for a in arts.get(r["uid"], [])]
                out.append(d)
            print(json.dumps(out, indent=1, default=str))
            return 0

        for r in rows:
            docs = []
            for a in arts.get(r["uid"], []):
                if a["kind"] == "jd_snapshot":
                    continue
                bad = [k for k, v in json.loads(a["gates"] or "{}").items()
                       if v is False]
                mark = f" {a['rating']:.0f}/100" if a["rating"] else ""
                if a["summary"]:
                    mark += f" {a['summary'][:40]}"
                mark += f" [{len(bad)} gate(s) failed]" if bad else ""
                docs.append(f"{a['kind']}{mark}")
            status = "" if r["status"] == "new" else f"  [{r['status']}]"
            _say(f"{r['score']:>5.0f}  {r['title'][:52]:<52} {r['company'][:22]:<22}"
                 f"  {r['salary_label'] or 'unconfirmed':<20}{status}")
            _say(f"       {r['uid']}  {r['location'][:60]}")
            if r["note"]:
                _say(f"       note: {r['note']}")
            if docs:
                _say(f"       {' | '.join(docs)}")
        _say(f"\n{len(rows)} role(s)")
        return 0
    finally:
        con.close()


# ---------------------------------------------------------------- serve
def cmd_serve(args) -> int:
    from . import store
    from .serve import serve
    # Checked here rather than inside `serve`, because the server opens a
    # connection per request: a bad `--db` would otherwise be found by the
    # first page load, after a browser had already been launched at a
    # dashboard that cannot answer. The connection is closed straight away;
    # this is a check on the path, not the server's own handle.
    store.connect(args.db, must_exist=True).close()
    return serve(db_path=args.db, host=args.host, port=args.port,
                 open_browser=not args.no_browser, docs_base=args.docs,
                 config_path=args.config)


# ---------------------------------------------------------------- setup
def _csv_list(raw: str | None) -> list[str]:
    """A comma or space separated flag value, as a list.

    Both separators, because the flags this parses take things people write
    both ways: `--countries UK,IE` and `--countries "UK IE"` are the same
    request, and answering one of them with a single country code called
    "UK IE" is the sort of wrong answer that then looks like a config nobody
    can find a fault in.
    """
    return [x.strip() for x in re.split(r"[,\s]+", raw or "") if x.strip()]


def cmd_setup(args) -> int:
    import inspect

    from .setup_wizard import run as wizard
    from .setup_wizard import NoInput

    # `--defaults` is the only path that works without a terminal, so it is
    # the whole of the story for scripts, CI and anyone setting this up over
    # ssh. It wrote `countries: [UK]` and `currency: GBP` with no flag able to
    # say otherwise, so somebody in Austin got a config filtering their
    # results to the wrong continent and pricing them in the wrong money, and
    # nothing on the way through said so.
    extra = {}
    if args.countries:
        extra["countries"] = [c.upper() for c in _csv_list(args.countries)]
    if args.currency:
        extra["currency"] = args.currency.strip().upper()
    # Goes through the same signature check as the rest, so a build whose
    # wizard predates the flag says so rather than fetching 130MB nobody
    # asked for, or silently not fetching what somebody did.
    extra["seed"] = not getattr(args, "no_seed", False)
    # Checked against the wizard rather than assumed, because the two halves
    # of these flags live in different modules -- the flag is declared here
    # and the answer is written there -- and a namespace that does not match
    # the parser has already shipped four times in this file. Passing a
    # keyword the wizard does not take is a TypeError in front of somebody
    # running setup for the first time; dropping it quietly is worse, because
    # they would get `countries: [UK]` in their config having explicitly typed
    # `--countries US`, which is the exact failure the flag exists to fix.
    takes = set(inspect.signature(wizard).parameters)
    unusable = sorted(f"--{k}" for k in extra if k not in takes)
    if unusable:
        _say(f"This build's setup wizard cannot apply {', '.join(unusable)}, "
             f"so nothing was written. A config that silently ignored them "
             f"would be worse than none.")
        return 1
    try:
        return wizard(_cfg_write_path(args.config),
                      non_interactive=args.defaults, cv=args.cv,
                      titles=args.titles, scan=getattr(args, "scan", False),
                      **extra)
    except NoInput:
        # stdin closed part-way through. The isatty guard in the wizard turns
        # most of these away at the door; this catches the rest, such as a pty
        # whose other end went away, so they end in a sentence rather than a
        # traceback or an unanswerable question asked forever.
        _say("\nInput ended before setup finished, so nothing was written.")
        return 1


# ---------------------------------------------------------------- main
def _limit(v: str) -> int:
    """A count of things to read. Never negative.

    `--limit` is applied as `srcs[:n]`, and a negative n is a slice from the
    END: `job-radar scan --limit -5` read 17,806 of 17,811 sources rather than
    5, spent 77 minutes doing it against other people's job boards, and then
    reported "only 17,806 of your 17,811 sources were read", which reads as a
    limit that worked. A stray minus sign is a plausible typo and the failure
    it produced was indistinguishable from success.
    """
    try:
        n = int(v)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{v!r} is not a whole number.")
    if n < 0:
        raise argparse.ArgumentTypeError(
            f"{n} is negative. --limit is how many to read, and a negative "
            f"number would read all but the last {abs(n)}. Use 0 for no limit.")
    return n


# Written once because it is on nine subcommands, and because the second
# sentence is the behaviour a reader has to be told: `list --db typo.db` used
# to answer `0 role(s)` and leave a 64KB file behind, which is the confident
# wrong answer this tool exists to not give.
_DB_HELP = ("database path (default data/job-radar.db). This command only "
            "reads, so it will not create one: a path that is not there is "
            "an error rather than an empty answer.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="job-radar",
        description="Watch employer job boards directly, and only be told about "
                    "roles that pass your own filters.",
    )
    p.add_argument("-c", "--config", default=None, help="config.yaml path")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="fetch every source and report matches")
    s.add_argument("-o", "--out", default=None,
                   help="directory for index.html, roles.json and roles.md "
                        "(default: `output.dir` from your config, or out/). "
                        "Checked for writability before the first request, "
                        "not after an hour of reading boards.")
    s.add_argument("--state", default=None,
                   help="the seen-set file, and the folder the "
                        "host-block memory sits in "
                        "(default state/seen.json). The database is the real "
                        "record; this is the export a CI runner commits.")
    s.add_argument("--db", default=None, help="database path (default data/job-radar.db)")
    s.add_argument("--limit", type=_limit, default=0,
                   help="read only the first N sources, for a quick look. "
                        "It is the head of the list, not a sample of it, so "
                        "a small N reads one platform's boards and can "
                        "match nothing. 0 reads all of them.")
    s.add_argument("--no-enrich", action="store_true",
                   help="skip fetching full postings for headline-only "
                        "sources; they stay unscreenable")
    s.add_argument("--no-open", action="store_true",
                   help="do not open the dashboard when the first pass "
                        "finishes. It is opened by default because pass one "
                        "takes about five minutes and the rest of the scan "
                        "takes over an hour.")
    s.add_argument("--no-caffeine", action="store_true",
                   help="do not hold the machine awake during the scan. It is "
                        "held by default because a full run is about an hour "
                        "and an idle laptop will otherwise sleep through it.")
    s.add_argument("--dry-run", action="store_true",
                   help="do not record what was seen (re-reports the same roles next time)")
    s.set_defaults(func=cmd_scan)

    d = sub.add_parser("discover", help="find a company's job board from its careers page")
    d.add_argument("targets", nargs="+", help="domain, careers URL, or company name")
    d.add_argument("--company", default=None,
                   help="the employer name to record. Without it the name is "
                        "guessed from the domain, which turns "
                        "boards.greenhouse.io/acmeco into \"Acmeco\".")
    d.add_argument("--add", action="store_true", help="write results into your config")
    d.add_argument("--no-validate", action="store_true",
                   help="do not fetch each board found to check it answers "
                        "and is really this employer's. Faster, and the "
                        "results are then guesses.")
    d.set_defaults(func=cmd_discover)

    sd = sub.add_parser("seed", help="prebuilt roles for the slow half of a scan")
    sdsub = sd.add_subparsers(dest="seed_cmd", required=True)
    sb = sdsub.add_parser("build", help="write a shard set (maintainers)")
    sb.add_argument("--out", default="seed", help="directory to write into")
    sb.add_argument("--limit", type=_limit, default=0, help="read only N boards")
    sb.set_defaults(func=cmd_seed_build)
    sl = sdsub.add_parser("load", help="import a shard set for your countries")
    sl.add_argument("path",
                    help="a directory holding index.json, or an https:// URL "
                         "the shard set was published under. A URL downloads "
                         "only the shards your countries need.")
    sl.add_argument("--keep", default=None,
                    help="where to keep a downloaded shard set (default: "
                         "seed/ beside your config). Only used with a URL.")
    sl.add_argument("--db", default=None,
                    help="database path (default data/job-radar.db)")
    sl.add_argument("--dry-run", action="store_true",
                    help="say what would be stored, write nothing")
    sl.set_defaults(func=cmd_seed_load)

    v = sub.add_parser("validate", help="check known sources are alive and are who they claim")
    v.add_argument("--file", default=None, help="a sources.json to check instead of the config set")
    v.add_argument("--report", default=None,
                   help="write the full result as JSON to this path. It is "
                        "the only durable trace of a run that costs hours of "
                        "network, and it is what --prune argues from.")
    v.add_argument("--limit", type=_limit, default=0,
                   help="check only the first N sources. 0 checks all of "
                        "them.")
    v.add_argument("--prune", action="store_true", help="rewrite --file without dead sources")
    v.add_argument("--force-prune", action="store_true",
                   help="prune even when most of the list came back empty, "
                        "which normally means the network is the problem")
    v.set_defaults(func=cmd_validate)

    c = sub.add_parser("coverage", help="where the source list is thin")
    c.add_argument("--file", default=None,
                   help="a sources.json to measure instead of the set your "
                        "config would scan")
    c.set_defaults(func=cmd_coverage)

    ap = sub.add_parser("applied", help="record what happened with a role")
    ap.add_argument("target", help="a posting URL, a company name, or a uid")
    ap.add_argument("-s", "--status", default="applied",
                    help="new|interested|applied|submitted|interviewing|offer|"
                         "rejected|withdrawn|skipped|closed")
    ap.add_argument("--note", default=None,
                   help="free text kept against the role, shown by `list` and "
                        "on the dashboard. Pass an empty string to clear one.")
    ap.add_argument("--db", default=None, help=_DB_HELP)
    ap.set_defaults(func=cmd_applied)

    g = sub.add_parser("generate", help="screen a role, or draft a CV or cover letter")
    g.add_argument("target", help="a posting URL, a company name, or a uid")
    g.add_argument("-k", "--kind", default="screen",
                   help="screen | cv | cover_letter")
    g.add_argument("--db", default=None, help=_DB_HELP)
    g.add_argument("--docs", default=None,
                   help="where the CV or cover letter is written "
                        "(default $JOB_RADAR_DOCS, or ~/job-applications)")
    g.add_argument("--force", action="store_true",
                   help="screen even when the posting has no description")
    g.set_defaults(func=cmd_generate)

    en = sub.add_parser("enrich",
                        help="fetch full postings for headline-only sources")
    en.add_argument("--limit", type=_limit, default=0,
                    help="fetch at most N postings. 0 fetches every one that "
                         "arrived as a headline only.")
    # Defaults to None, not to a number, so "the user asked for a pause" can
    # be told apart from "nobody said". A pause is now a request to go one at
    # a time: each host is paced on its own clock, so a blanket delay between
    # unrelated servers only costs time.
    en.add_argument("--pause", type=float, default=None,
                    help="seconds between requests, which also forces them to "
                         "run one at a time. Without it each host is paced "
                         "separately and different hosts run in parallel.")
    en.add_argument("--concurrency", type=int, default=None,
                    help="how many postings to fetch at once (ignored with --pause)")
    en.add_argument("--dry-run", action="store_true",
                    help="say how many postings would be fetched and fetch "
                         "none of them")
    en.add_argument("--db", default=None, help=_DB_HELP)
    en.set_defaults(func=cmd_enrich)

    rk = sub.add_parser("rank", help="score every role against your CV, cheaply")
    rk.add_argument("--refresh", action="store_true",
                    help="re-score roles that already have a fit")
    rk.add_argument("--limit", type=_limit, default=0,
                    help="score at most N roles, which is also how you cap "
                         "the spend. 0 scores every unranked one.")
    rk.add_argument("--top", type=int, default=12, help="how many to print")
    rk.add_argument("--dry-run", action="store_true",
                    help="show what it would cost and send nothing")
    rk.add_argument("--db", default=None, help=_DB_HELP)
    rk.set_defaults(func=cmd_rank)

    rs = sub.add_parser("rescreen",
                        help="re-apply your config to roles already stored")
    rs.add_argument("--remove", action="store_true",
                    help="delete the ones that no longer match and that you "
                         "have not acted on")
    rs.add_argument("--limit", type=_limit, default=0, help="how many to list")
    rs.add_argument("--db", default=None, help=_DB_HELP)
    rs.set_defaults(func=cmd_rescreen)

    ls = sub.add_parser("list", help="the dashboard, as text")
    ls.add_argument("--status", default=None,
                    help="only roles at this status: "
                         "new|interested|applied|submitted|interviewing|offer|"
                         "rejected|withdrawn|skipped|closed. Naming a settled "
                         "one shows it, which is otherwise what --all is for.")
    ls.add_argument("--all", action="store_true",
                    help="include settled roles and ones no longer on a board")
    ls.add_argument("--new", action="store_true",
                    help="only roles first seen on the most recent scan")
    ls.add_argument("--limit", type=_limit, default=0,
                    help="print at most N roles, best score first. 0 prints "
                         "all of them.")
    ls.add_argument("--json", action="store_true",
                    help="print the same roles as JSON, one object each, with "
                         "their notes and generated documents attached")
    ls.add_argument("--db", default=None, help=_DB_HELP)
    ls.set_defaults(func=cmd_list)

    sv = sub.add_parser("serve", help="open the dashboard you can act from")
    sv.add_argument("--db", default=None, help=_DB_HELP)
    sv.add_argument("--host", default="127.0.0.1",
                    help="address to listen on (default 127.0.0.1). This "
                         "database holds your application history and private "
                         "notes, so anything other than a loopback address "
                         "publishes it to your network.")
    sv.add_argument("--port", type=int, default=8765,
                    help="port to listen on (default 8765). The scan opens "
                         "the dashboard on this port too, so a server already "
                         "holding it is why you would change this.")
    sv.add_argument("--docs", default=None, help="where generated documents go")
    sv.add_argument("--no-browser", action="store_true",
                    help="start the server without opening a browser at it")
    sv.set_defaults(func=cmd_serve)

    w = sub.add_parser("setup", help="build a config by answering a few questions")
    w.add_argument("--defaults", action="store_true", help="write a default config, ask nothing")
    w.add_argument("--no-seed", action="store_true",
                   help="do not fetch the published seed. Setup fetches it by "
                        "default because it is the slow three quarters of a "
                        "scan already read, and it lands in about a minute "
                        "where a scan takes an hour.")
    w.add_argument("--scan", action="store_true",
                   help="with --defaults, run the first scan too. The "
                        "interactive wizard always does.")
    w.add_argument("--cv", default=None, help="path to your CV (required with --defaults)")
    w.add_argument("--titles", default=None,
                   help="comma-separated job titles (required with --defaults)")
    w.add_argument("--countries", default=None,
                   help="country codes you can work in, comma or space "
                        "separated, e.g. 'US' or 'UK,IE'. With --defaults "
                        "this is the only way to say so: it wrote UK for "
                        "everybody.")
    w.add_argument("--currency", default=None,
                   help="currency your salary floor is in, e.g. USD. With "
                        "--defaults this is the only way to say so: it wrote "
                        "GBP for everybody.")
    w.set_defaults(func=cmd_setup)

    return p


def main(argv=None) -> int:
    from .store import StoreError
    args = build_parser().parse_args(argv)
    try:
        # Before the command, so it is read rather than scrolled past at the
        # end of two hundred lines of scan output.
        if args.cmd in ("scan", "list", "serve", "rank", "coverage"):
            # Reading the config is NOT part of the nudge, and it is outside
            # the catch-all for that reason. A nudge must never stop the
            # command; a config that cannot be read is not a failed nudge, it
            # is the reason the command is about to give a wrong answer.
            # `list` never loads the config itself, so swallowing this was the
            # difference between "sectors: [manufacturing] is not a sector
            # that exists" and a confident `0 role(s)`.
            #
            # It used to be only ConfigError that was let through, which meant
            # the two ways a config fails BEFORE it can be validated -- YAML
            # that does not parse, and `-c` pointing at a directory -- landed
            # in the catch-all instead. `list -c broken.yaml` printed
            # `0 role(s)` and exited 0. A file the tool cannot read is the
            # loudest possible config problem and it was the only silent one.
            cfg_for_nudge = _cfg_or_default(args.config)
            try:
                _daily_sync_nudge(cfg_for_nudge, getattr(args, "db", None))
            except Exception:
                pass          # a nudge must never stop the command
        return args.func(args)
    except StoreError as e:
        # A `--db` this tool cannot use. Every one of these used to arrive as
        # a raw sqlite exception under a nine-frame traceback: a directory and
        # an unwritable folder both read `unable to open database file`, and a
        # text file read `file is not a database`, none of which name the path
        # or the fix. `store` writes the sentence; this prints it and nothing
        # else.
        _say(str(e))
        return 1
    except FileNotFoundError as e:
        _say(str(e))
        return 1
    except ConfigError as e:
        # A config mistake is the user's to fix, and a traceback buries the
        # one line that tells them how.
        _say(f"Problem in your config: {e}")
        return 1
    except IsADirectoryError as e:
        # `-c cfg/` rather than `-c cfg/config.yaml`. Caught separately from
        # FileNotFoundError above, which it is not a subclass of.
        _say(f"{e}\n-c wants the config file itself, not the directory it "
             f"is in.")
        return 1
    except yaml.YAMLError as e:
        # The file exists and is not YAML. yaml's own message names the line
        # and column, which is the whole of the useful part; the traceback
        # above it is none of it.
        _say(f"Your config is not valid YAML, so none of your settings were "
             f"read:\n{e}")
        return 1
    except KeyboardInterrupt:
        _say("\nstopped")
        return 130


if __name__ == "__main__":
    sys.exit(main())
