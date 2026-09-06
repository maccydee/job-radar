"""The scan's cost, and the shape changes made to reduce it.

Nothing in here asserts on a duration. A timing test is flaky by
construction -- it fails on a slow runner and passes on a fast one whatever
the code does -- and this suite has already lost a week to one. So the
measurements that motivated these changes live in the report and in
`tools/bench_fetch.py --platforms`, and what is pinned here is the BEHAVIOUR
that made the saving possible and the behaviour that must survive it.

The numbers behind the changes, for anyone reading this later and wondering
why the code is shaped this way. Measured on 2026-08-26, per-platform samples
against the real hosts:

  * apply.workable.com is the whole scan's floor: 2,094 boards, one request
    each, paced at 0.7 requests a second, which is 49.9 minutes before
    anything else happens. The requests themselves take 0.21s, so essentially
    all of that is deliberate waiting.
  * Screening cost 0.92ms a posting against roughly 480,000 postings, and 94%
    of it was spent enriching postings that the very next line then dropped on
    a title mismatch.
  * Parsing cost 262 microseconds a posting: about 126 seconds a scan, which
    used to run after the fetch and now runs inside it.
  * Enrichment ran at 8.1 postings a second, and the last full scan enriched
    958 of them. Two minutes of a fifty minute run.
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import cli
from jobradar.fetch import Result
from jobradar.models import Job, Source


# ---------------------------------------------------------------------------
# Parsing during the fetch rather than after it
# ---------------------------------------------------------------------------

def _cfg(d: Path, extra: str = "") -> Path:
    p = d / "config.yaml"
    p.write_text(
        "titles:\n  include: [engineering manager]\n"
        "output:\n  formats: []\n  dir: " + str(d / "out") + "\n"
        "sources:\n  use_bundled: false\n  extra:\n"
        "    - company: One\n      url: https://boards.greenhouse.io/one\n"
        "    - company: Two\n      url: https://boards.greenhouse.io/two\n"
        + extra, encoding="utf-8")
    return p


def _args(d: Path, cfg: Path):
    class A:
        config = str(cfg)
        db = str(d / "x.db")
        state = str(d / "seen.json")
        out = str(d / "out")
        docs = None
        dry_run = False
        no_enrich = True
        no_caffeine = True
        resume = False
        no_open = True
        limit = 0
    return A


def _job(src, location="London"):
    return [Job(company=src.company, title="Engineering Manager",
                url=f"https://x.invalid/{src.company}", platform="greenhouse",
                location=location)]


@contextlib.contextmanager
def _stub_fetch(fetch_all, parse=None):
    """Swap the two things `cmd_scan` reaches out through, and put them back."""
    real_fetch, real_parse = cli.fetch_all, cli.adapters.parse
    cli.fetch_all = fetch_all
    cli.adapters.parse = parse or (lambda payload, src: _job(src))
    try:
        yield
    finally:
        cli.fetch_all, cli.adapters.parse = real_fetch, real_parse


def test_the_scan_parses_each_source_before_the_fetch_has_finished():
    """The point of the change, stated as behaviour rather than as a stopwatch.

    Parsing used to be a second pass over `results` after `fetch_all` returned.
    The scan's floor is not this machine -- it is apply.workable.com's 0.7
    requests a second, 50 minutes for 2,094 boards -- so the thread that
    collects results sits idle for most of the run. Doing the parsing there
    puts about two minutes of work inside time already being spent waiting.

    Asserted by having the stub `fetch_all` check, before it returns, that the
    parsing has already happened. If parsing moved back out to a second pass
    this is 0 at that moment and the test fails.
    """
    parsed: list = []
    seen_before_return = {}

    def fetch_all(srcs, **kw):
        out = []
        for s in srcs:
            r = Result(source=s, payload=b"[]")
            out.append(r)
            kw["on_result"](r)
        seen_before_return["n"] = len(parsed)
        return out

    def parse(payload, src):
        parsed.append(src.company)
        return _job(src)

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        with _stub_fetch(fetch_all, parse):
            with contextlib.redirect_stdout(io.StringIO()):
                assert cli.cmd_scan(_args(d, _cfg(d))) == 0

    assert seen_before_return["n"] == 2, (
        f"fetch_all returned with only {seen_before_return['n']} of 2 sources "
        f"parsed; the parsing is happening after the fetch again")


def test_a_fetch_all_that_never_calls_on_result_still_scans_every_source():
    """`on_result` is an optimisation, not a contract.

    `fetch_all`'s callback is optional, and a caller that simply returns its
    results is a reasonable thing to write -- tests in this suite stub exactly
    that. When the parsing moved into the callback, such a caller silently
    scanned zero postings and printed "Nothing matched" on a full board, which
    is the failure-that-looks-like-success this codebase keeps producing. So
    there is a sweep afterwards, and this is what holds it in place.
    """
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        with _stub_fetch(lambda srcs, **kw: [Result(source=s, payload=b"[]")
                                             for s in srcs]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                assert cli.cmd_scan(_args(d, _cfg(d))) == 0
    text = out.getvalue()
    assert "2/2 responded, 2 postings" in text, text
    assert "Nothing matched" not in text, text


def test_a_source_is_never_counted_twice_when_both_paths_run():
    """The sweep runs over results the callback has already taken in.

    Both paths fire on every real scan, so `absorb` has to be idempotent. If
    it were not, `ok`, the posting count and `all_jobs` would each double, and
    an inflated posting count is exactly the kind of number nobody checks.
    """
    def fetch_all(srcs, **kw):
        out = []
        for s in srcs:
            r = Result(source=s, payload=b"[]")
            out.append(r)
            kw["on_result"](r)          # and cmd_scan sweeps the same objects
        return out

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        with _stub_fetch(fetch_all):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                assert cli.cmd_scan(_args(d, _cfg(d))) == 0
    text = out.getvalue()
    assert "2/2 responded, 2 postings" in text, text
    assert "4/2" not in text and "4 postings" not in text, text


def test_a_source_that_failed_is_not_parsed_or_counted():
    """A failed result has no payload. Parsing one is a crash, counting one is
    a lie about how many boards answered."""
    def fetch_all(srcs, **kw):
        out = []
        for i, s in enumerate(srcs):
            r = (Result(source=s, payload=b"[]") if i == 0
                 else Result(source=s, error="HTTP 500", status=500))
            out.append(r)
            kw["on_result"](r)
        return out

    def parse(payload, src):
        assert payload is not None, "parsed a result that had no payload"
        return _job(src)

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        with _stub_fetch(fetch_all, parse):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                assert cli.cmd_scan(_args(d, _cfg(d))) == 0
    assert "1/2 responded" in out.getvalue(), out.getvalue()


# ---------------------------------------------------------------------------
# The country attribution that moved with the parsing
# ---------------------------------------------------------------------------

def _country_after_scan(location: str, tag: str | None) -> str | None:
    """Run one posting through the scan and report the country it was stored with."""
    got: list = []

    def parse(payload, src):
        return [Job(company=src.company, title="Engineering Manager",
                    url="https://x.invalid/a", platform="greenhouse",
                    location=location)]

    def fetch_all(srcs, **kw):
        out = []
        for s in srcs:
            s.country = tag
            r = Result(source=s, payload=b"[]")
            out.append(r)
            kw["on_result"](r)
        return out

    real_screen = cli.screen_run

    def screen_run(jobs, cfg):
        # The country as the SCAN stored it. Read after the fact it would be
        # whatever `screen.enrich` later rewrote it to: that fills an empty
        # country with "multiple", so "Berlin / Paris" on a UK board would
        # read back as "multiple" and the assertion would be about the wrong
        # function entirely.
        got.extend(j.country for j in jobs)
        return real_screen(jobs, cfg)

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        cli.screen_run = screen_run
        try:
            with _stub_fetch(fetch_all, parse):
                with contextlib.redirect_stdout(io.StringIO()):
                    cli.cmd_scan(_args(d, _cfg(d)))
        finally:
            cli.screen_run = real_screen
    return got[0] if got else None


def test_the_boards_country_tag_is_only_a_fallback_and_never_an_override():
    """Homebase's board is tagged UK because it is a UK retailer, and a genuine
    Toronto vacancy on it was being stored as UK. The posting's own location
    wins; the tag only fills a silence."""
    assert _country_after_scan("Toronto, ON", "UK") == "CA"
    assert _country_after_scan("London", "UK") == "UK"
    # Names nowhere, so the tag is all there is.
    assert _country_after_scan("", "UK") == "UK"


def test_a_tag_that_is_not_a_country_is_never_stored_as_one():
    """`multi` is a note about the board, not a place. Storing it as a country
    puts a country nobody can filter on into the database."""
    assert _country_after_scan("", "multi") in ("", None)


def test_a_posting_naming_several_countries_keeps_the_tag_only_if_it_is_one_of_them():
    """"London / New York" on a UK board really is partly UK. "Berlin / Paris"
    is not, however the board is tagged."""
    assert _country_after_scan("London / New York", "UK") == "UK"
    assert _country_after_scan("Berlin / Paris", "UK") == ""


# ---------------------------------------------------------------------------
# Enrichment: the tidy-up that would have been a regression
# ---------------------------------------------------------------------------

def test_the_enrichment_pass_is_not_wired_to_the_configured_concurrency():
    """This looks like an oversight and is a deliberate decision.

    `enrich.run` falls back to `fetch.DEFAULT_CONCURRENCY`, so the scan honours
    `fetch.concurrency` and this step does not. Wiring them together is the
    obvious tidy-up and it makes things worse: plenty of configs still carry
    the `concurrency: 4` the old advice recommended, and those runs would drop
    from 16 enrichment workers to 4. Measured, the whole step is 8.1 postings
    a second and the last full scan enriched 958 of them -- about two minutes
    of a fifty minute run -- so there is nothing here worth the risk.
    """
    tree = ast.parse(inspect.getsource(cli._enrich_step))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "run"]
    assert calls, "_enrich_step no longer calls enrich.run"
    for c in calls:
        kw = {k.arg for k in c.keywords}
        assert "concurrency" not in kw, (
            "_enrich_step now passes concurrency to enrich.run. That takes a "
            "config with fetch.concurrency: 4 from 16 enrichment workers down "
            "to 4, turning a two minute step into eight.")


# ---------------------------------------------------------------------------
# The measuring tool itself
# ---------------------------------------------------------------------------

def _bench():
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "tools" / "bench_fetch.py"
    spec = importlib.util.spec_from_file_location("bench_fetch", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_projection_names_the_host_the_scan_cannot_finish_before():
    """The whole point of the per-platform mode: a board count hides this.

    Workable holds 2,094 of 17,810 sources, 12% of the list, and is 100% of
    the floor, because it is the one busy host paced below the default. Run
    entirely from fixture rates, with no network.
    """
    bench = _bench()
    srcs = ([Source(company=f"w{i}", url=f"https://apply.workable.com/api/v1/widget/accounts/w{i}",
                    platform="workable") for i in range(2094)]
            + [Source(company=f"g{i}", url=f"https://boards-api.greenhouse.io/v1/boards/g{i}/jobs",
                      platform="greenhouse") for i in range(4078)])
    rows = {"workable": {"req_per_board": 1.0, "latency_mean": 0.213},
            "greenhouse": {"req_per_board": 1.0, "latency_mean": 1.288}}
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        bench.project(rows, srcs)
    text = out.getvalue()
    assert "apply.workable.com" in text, text
    # Named as the floor even though Greenhouse has twice the boards and six
    # times the latency, because Greenhouse is paced at 3/s and Workable at 0.7.
    last = text.strip().splitlines()[-1]
    assert "cannot finish faster" in last and "apply.workable.com" in last, last


def test_the_benchmark_reads_only_a_handful_of_workable_boards():
    """A benchmark is not a reason to spend the politeness budget.

    apply.workable.com carries a standing quota rather than a rate limit: a
    sustained 1.5 requests a second answered 429 at request 301 on 2026-08-26,
    and the host has previously stayed shut for sixteen hours. Every other
    platform here can be sampled freely; this one cannot, and a default that
    quietly reads hundreds of its boards would arm that trap on whoever runs
    the benchmark next.
    """
    bench = _bench()
    assert bench.PLATFORM_SAMPLE["workable"] <= 20, (
        "the per-platform benchmark reads "
        f"{bench.PLATFORM_SAMPLE['workable']} Workable boards by default")


def test_the_benchmark_times_the_request_and_not_the_wait_for_it():
    """`Result.elapsed` starts before `HostLimiter.wait`, so on a paced host it
    is mostly the pacing: about 1.4s for a Workable request that takes 0.21s.
    Reading it here would blame the network for a delay this tool chose, and
    the entire finding is that Workable's 50 minutes are self-imposed."""
    import ast
    import textwrap

    bench = _bench()
    # The docstring explains exactly this trap, so it has to come out before
    # looking, or the test fails on its own explanation of why it exists.
    #
    # Removed by parsing rather than by `src.replace(__doc__, "")`, which
    # worked everywhere until Python 3.13 and then failed only there: 3.13
    # dedents docstrings when it stores them, so `__doc__` no longer appears
    # verbatim in the indented source and the replace silently did nothing.
    tree = ast.parse(textwrap.dedent(inspect.getsource(bench.measure_platforms)))
    fn = tree.body[0]
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)
            and isinstance(fn.body[0].value.value, str)):
        fn.body = fn.body[1:]
    src = ast.unparse(fn)
    assert ".elapsed" not in src, (
        "measure_platforms is reading Result.elapsed, which on a paced host "
        "reports the limiter wait as though it were latency")
    assert "Session.get" in src and "Session.post" in src, src


def test_the_title_gate_runs_before_the_expensive_enrichment():
    """`enrich` resolves country, city, work mode and work rights, which is
    85% of screening CPU, and it was being run on every posting before the
    filter that discards more than 99% of them.

    Asserted on the order rather than on a clock, because a timing test is
    flaky by construction. The order is load-bearing in both directions:
    `match` must not need anything `enrich` sets, and `sponsorship_gate` reads
    `job.country` while `apply_salary` and `screen` both append to
    `job.flags`, so `enrich` has to sit between them.
    """
    import inspect
    import re

    from jobradar import screen as screen_mod

    body = inspect.getsource(screen_mod.run)
    order = [m.group(1) for m in re.finditer(
        r"\b(enrich|match|apply_salary|screen|sponsorship_gate|score)\(j",
        body)]
    assert order.index("match") < order.index("enrich"), (
        "enrich runs on postings the title filter is about to discard")
    assert order.index("enrich") < order.index("sponsorship_gate"), (
        "sponsorship_gate reads job.country, which enrich sets")
    assert order.index("enrich") < order.index("apply_salary"), (
        "apply_salary appends to job.flags, which enrich also appends to")

    # And the safety condition the swap rests on: match must not read a field
    # enrich fills in. If someone adds one, this fails rather than silently
    # changing which roles are kept.
    sets = set(re.findall(r"job\.([a-z_]+)\s*=",
                          inspect.getsource(screen_mod.enrich)))
    reads = inspect.getsource(screen_mod.match)
    leaked = [a for a in sets if re.search(rf"\b(?:j|job|role)\.{a}\b", reads)]
    assert not leaked, f"match now reads {leaked}, which enrich sets after it"
