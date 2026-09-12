"""The loose ends earlier audits recorded and could not reach.

Four faults, each one left in a comment by the audit that found it, and each
one reproduced here as the input that produces it before it is guarded:

  * one source with a placeholder nothing can fill in killed a whole
    `validate` run from inside `ThreadPoolExecutor.map`, so every source after
    it went unchecked;
  * `--prune` decided what to delete from the row's verdict while ignoring the
    `prunable` flag that exists to say so;
  * `store.merge_duplicates` deleted the losing row outright, so a role open
    in two cities lost the second one, while `screen.dedupe` joined them;
  * the shipped source list carried two spellings of one country tag.

Nothing here touches the network: the unusable URL raises before a request is
built, and the `validate` cases replace the per-source check with a stand-in.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import cli, sources as src_mod, store        # noqa: E402
from jobradar import discover as disc                      # noqa: E402
from jobradar.models import Job, Source                    # noqa: E402
from jobradar.screen import dedupe, merged_location        # noqa: E402


class _Tmp:
    """A throwaway directory that cleans itself up."""

    def __enter__(self):
        self._d = tempfile.TemporaryDirectory()
        return Path(self._d.name)

    def __exit__(self, *exc):
        self._d.cleanup()
        return False


# The URL that started it: a hand-written source carrying a second placeholder
# this tool knows nothing about.
ODD_URL = "https://boards.example.test/api/jobs?q={keyword}&loc={location}"


def _config(tmp: Path) -> Path:
    """A real config file, so nothing under test reaches for the personal one.

    `_cfg_or_default(None)` falls back to whatever config is sitting in the
    working directory, which on a real machine is somebody's private one.
    """
    p = tmp / "config.yaml"
    p.write_text("titles:\n  include: [engineering manager]\n"
                 "sources:\n  use_bundled: false\n", encoding="utf-8")
    return p


def _source_file(tmp: Path, entries: list[dict]) -> Path:
    p = tmp / "sources.json"
    p.write_text(json.dumps({"sources": entries}), encoding="utf-8")
    return p


def _validate_args(tmp: Path, file: Path, **over) -> argparse.Namespace:
    args = argparse.Namespace(config=str(_config(tmp)), file=str(file),
                              limit=None, report=str(tmp / "report.json"),
                              prune=False, force_prune=False,
                              # Its own, so a test never reads or writes the
                              # developer's real record of which boards have
                              # been empty and for how long.
                              state_dir=str(tmp / "state"))
    for k, v in over.items():
        setattr(args, k, v)
    return args


# --------------------------------------------------- 1. the unusable URL

def test_a_url_placeholder_nothing_can_fill_in_is_named_rather_than_raised():
    """`{location}` is not `{keyword}` and not `{country}`, so there is
    nothing to put there. The old code called `.format` and let the KeyError
    out.

    This test used to ASSERT that `validate_source` still raised, calling the
    trap "proof that the guard is load-bearing rather than decorative". It was
    load-bearing somewhere else and absent here, and the trap it documented
    then killed the weekly source validation every run: `Workable search`
    carries `location={country}`, `str.format` raised `KeyError: 'country'`,
    the error came back out of `ThreadPoolExecutor.map`, and every source
    after it went unchecked. The workflow filed an issue each week blaming a
    throttled runner.

    So the assertion is inverted rather than deleted. A bad URL is now one
    source's problem, reported on its own row, and the rest of the list is
    still checked. See tests/test_validate_templated_sources.py.
    """
    odd = Source(company="Odd board", url=ODD_URL, platform="",
                 keyword_template=True)

    row = disc.validate_source(odd)
    assert row["verdict"] == "unreachable", row
    # Never prunable: the tool could not ask the question, which is not the
    # board having no answer, and `--prune` deletes on "dead".
    assert row["prunable"] is False, row
    assert "location" in row["note"], row["note"]

    why = src_mod.url_template_error(odd)
    assert why and "location" in why, why
    assert "keyword" in why and "country" in why, \
        f"the message has to say what it does know: {why}"


def test_a_literal_brace_in_an_ordinary_board_url_is_not_called_a_fault():
    """Nothing formats a plain board URL, so a brace in a query string is
    harmless, and reporting it would take a live board out of the health
    check."""
    plain = Source(company="Braces", platform="",
                   url='https://example.test/search?filter={"team":1}')
    assert src_mod.url_template_error(plain) is None


def test_validate_checks_every_source_after_an_unusable_one():
    """One bad source used to end the run. The KeyError came back out of
    `ex.map`, `cmd_validate` was consuming that in a bare `for`, and the
    thousands of sources queued behind it were never checked -- with no line
    in the output saying a health check had stopped half way."""
    checked: list = []

    def fake_validate(src):
        checked.append(src.company)
        return {"company": src.company, "url": src.url, "platform": "",
                "live_jobs": 3, "verdict": "live", "transport": None,
                "prunable": False, "note": ""}

    real = cli.validate_source
    cli.validate_source = fake_validate
    try:
        with _Tmp() as tmp:
            f = _source_file(tmp, [
                {"company": "First", "url": "https://a.example.test/jobs"},
                {"company": "Odd board", "url": ODD_URL,
                 "keyword_template": True},
                {"company": "Last", "url": "https://z.example.test/jobs"},
            ])
            rc = cli.cmd_validate(_validate_args(tmp, f))
            report = json.loads((tmp / "report.json").read_text(encoding="utf-8"))
    finally:
        cli.validate_source = real

    assert rc == 0
    assert "Last" in checked, \
        "the source after the unusable one was never checked"
    assert report["total"] == 3, report["total"]
    odd = [r for r in report["rows"] if r["company"] == "Odd board"][0]
    assert odd["verdict"] == "unreachable", odd
    assert odd["prunable"] is False, "an unreadable URL is not a dead board"
    assert "location" in odd["note"], odd["note"]


def test_one_unusable_template_does_not_stop_the_whole_source_list_loading():
    """The same `.format` sits in `expand_templates`, where it is worse: it
    runs before a single board is read, so every command that loads sources
    died on one bad line in `sources.extra`."""

    class _Cfg:
        use_bundled_sources = False
        extra_sources = [
            {"company": "Odd board", "url": ODD_URL, "keyword_template": True},
            {"company": "Fine search", "keyword_template": True,
             "url": "https://ok.example.test/s?q={keyword}"},
        ]
        sectors: list = []
        source_countries: list = []
        countries = ["GB"]
        relocate_to: list = []
        titles_include = ["engineering manager"]

    problems: list = []
    out = src_mod.load(_Cfg(), problems=problems)
    assert [s.company for s in out] == ["Fine search: engineering manager"]
    assert len(problems) == 1 and problems[0][0] == "Odd board"
    assert "location" in problems[0][1]


# ------------------------------------------------- 2. prune reads the flag

def _row(company, url, verdict, prunable, transport=None):
    return {"company": company, "url": url, "platform": "", "live_jobs": 0,
            "verdict": verdict, "transport": transport, "prunable": prunable,
            "note": ""}


def test_prune_deletes_on_the_prunable_flag_and_not_on_the_verdict():
    """The flag was added precisely so a board nobody could reach is never
    deletable, and the caller was still arguing from `verdict`. It agreed by
    luck. A row that says dead and says it must not be pruned is the case
    where luck runs out, and the cost is a live employer deleted from the
    shipped list."""
    urls = {"gone": "https://gone.example.test/jobs",
            "tls": "https://tls.example.test/jobs",
            "live": "https://live.example.test/jobs"}

    def fake_validate(src):
        if src.url == urls["gone"]:
            return _row(src.company, src.url, "dead", True)
        if src.url == urls["tls"]:
            # Answered nothing, but nothing reached the board either.
            return _row(src.company, src.url, "dead", False,
                        transport="UNEXPECTED_EOF_WHILE_READING")
        return _row(src.company, src.url, "live", False)

    real = cli.validate_source
    cli.validate_source = fake_validate
    try:
        with _Tmp() as tmp:
            entries = [{"company": "Gone", "url": urls["gone"]},
                       {"company": "Handshake", "url": urls["tls"]}]
            entries += [{"company": f"Live {i}",
                         "url": f"{urls['live']}?{i}"} for i in range(8)]
            f = _source_file(tmp, entries)
            args = _validate_args(tmp, f, prune=True)

            # `Gone` has to have been empty for weeks before anything will
            # delete it, so give it that history. An empty board is a company
            # with nothing open until it stays empty: measured on a real run,
            # about 8% of boards called dead on one Sunday were serving jobs
            # five days later. See jobradar/deadwood.py.
            #
            # Seeded rather than asserted away, because what THIS test is
            # about is the other row: a board nobody could reach must survive
            # the prune however its verdict reads.
            from datetime import date, timedelta
            from jobradar import deadwood
            log = deadwood.EmptyLog(Path(args.state_dir) / deadwood.DEFAULT_NAME)
            for back in (60, 40, 20):
                log.record(urls["gone"], True,
                           when=(date.today() - timedelta(days=back)).isoformat())
                log.record(urls["tls"], True,
                           when=(date.today() - timedelta(days=back)).isoformat())
            log.save()

            cli.cmd_validate(args)
            left = json.loads(f.read_text(encoding="utf-8"))
    finally:
        cli.validate_source = real

    kept = {s["company"] for s in left["sources"]}
    assert "Gone" not in kept, "a genuinely dead board should have gone"
    assert "Handshake" in kept, \
        "a board that was never reached must survive the prune"
    assert len(kept) == 9


def test_a_row_from_an_older_report_without_the_flag_still_falls_back():
    """`discover.prunable` is the one rule, and the caller now uses it, so a
    report written before the flag existed is read the same way."""
    assert disc.prunable({"verdict": "dead"}) is True
    assert disc.prunable({"verdict": "dead", "transport": "BAD_RECORD_MAC"}) is False
    assert disc.prunable({"verdict": "unreachable"}) is False
    assert disc.prunable({"verdict": "dead", "prunable": False}) is False


# ------------------------------------------- 3. the merge keeps both cities

def _pair(con, locations=("London", "New York"),
          platforms=("greenhouse", "greenhouse")):
    for i, (loc, plat) in enumerate(zip(locations, platforms)):
        con.execute(
            "INSERT INTO roles (uid,company,title,url,platform,location,"
            "description,first_seen,last_seen) VALUES (?,'Monzo',"
            "'Backend Engineer',?,?,?,?,date('now'),date('now'))",
            (f"u{i}", f"https://example.test/{i}", plat, loc, "x" * (600 - i)))
        con.execute("INSERT INTO role_state (uid,status,updated_at) "
                    "VALUES (?,'new',date('now'))", (f"u{i}",))


def test_merging_a_duplicate_keeps_the_other_citys_name():
    """Two Greenhouse postings, one title, London and New York. The merge
    deleted the loser outright, so New York left the dashboard the day the
    second posting arrived."""
    with _Tmp() as tmp:
        con = store.connect(tmp / "jobs.db")
        _pair(con)
        assert store.merge_duplicates(con) == 1
        row = con.execute("SELECT location, flags FROM roles").fetchone()
        loc, flags = row["location"], json.loads(row["flags"])
        con.close()
    assert "London" in loc and "New York" in loc, loc
    assert "posted in 2 locations" in flags, flags


def test_the_scan_pass_and_the_database_pass_write_the_same_location_line():
    """The two paths answer the same question and used to answer it
    differently depending on whether the copies arrived in one scan or in
    two. Same input, same line, or the dashboard changes with the timing."""
    jobs = [Job(company="Monzo", title="Backend Engineer",
                url="https://example.test/0", platform="greenhouse",
                location="London", description="x" * 600),
            Job(company="Monzo", title="Backend Engineer",
                url="https://example.test/1", platform="greenhouse",
                location="New York", description="x" * 599)]
    from_scan = dedupe(jobs)
    assert len(from_scan) == 1

    with _Tmp() as tmp:
        con = store.connect(tmp / "jobs.db")
        _pair(con)
        store.merge_duplicates(con)
        row = con.execute("SELECT location, flags FROM roles").fetchone()
        from_db, flags = row["location"], json.loads(row["flags"])
        con.close()

    assert from_db == from_scan[0].location, (from_db, from_scan[0].location)
    assert "posted in 2 locations" in flags
    assert "posted in 2 locations" in from_scan[0].flags


def test_merging_the_same_role_twice_neither_nests_nor_repeats_itself():
    """This runs unattended on every scan. A joined line fed back through the
    join would read "London / New York / London / New York", and a flag
    appended per run would stack up on the dashboard."""
    with _Tmp() as tmp:
        con = store.connect(tmp / "jobs.db")
        _pair(con)
        store.merge_duplicates(con)
        first = con.execute("SELECT location FROM roles").fetchone()["location"]
        # A third copy arrives on a later scan, in a city already listed.
        con.execute(
            "INSERT INTO roles (uid,company,title,url,platform,location,"
            "description,first_seen,last_seen) VALUES ('u2','Monzo',"
            "'Backend Engineer','https://example.test/2','linkedin','London',"
            "'y',date('now'),date('now'))")
        store.merge_duplicates(con)
        row = con.execute("SELECT location, flags FROM roles").fetchone()
        again, flags = row["location"], json.loads(row["flags"])
        con.close()
    assert first == "London / New York", first
    assert again == first, again
    assert flags.count("posted in 2 locations") == 1, flags


def test_a_location_line_that_was_already_merged_is_taken_apart_again():
    """`merged_location` is fed its own output on every later merge."""
    text, n = merged_location(["London / New York", "Berlin"])
    assert text == "London / New York / Berlin" and n == 3
    # The names behind a "+N more" tail are gone; the tail is dropped rather
    # than counted, because inventing a number for them would be worse.
    text, n = merged_location(["A / B / C / D / E / F +2 more", "G"])
    assert text.endswith("+1 more") and n == 7, (text, n)


# --------------------------------------------- 4. one spelling, one tag

def test_the_shipped_source_list_holds_one_spelling_of_every_country_tag():
    """`multi` and `multiple` both shipped, and `cli.py` read both, so
    nothing was broken and nothing said which was right. A third spelling
    added by the next harvest would be read by nothing at all."""
    raw = json.loads(src_mod.BUNDLED.read_text(encoding="utf-8"))
    tags = {(s.get("country") or "") for s in raw["sources"]}
    odd = {t for t in tags
           if t and t not in src_mod.NON_COUNTRY_TAGS
           and not (len(t) == 2 and t.isalpha() and t.isupper())}
    assert not odd, f"country tags that are neither a code nor a known tag: {odd}"
    assert "multiple" not in tags
    # Every tag in the file is already the canonical spelling of itself, so
    # loading the file changes nothing.
    for t in tags:
        assert src_mod.normalise_country_tag(t) == t, t


def test_a_third_spelling_cannot_reach_anything_that_reads_the_tag():
    assert src_mod.normalise_country_tag("multiple") == src_mod.MULTI_COUNTRY
    assert src_mod.normalise_country_tag("Global") == src_mod.MULTI_COUNTRY
    assert src_mod.normalise_country_tag("worldwide") == src_mod.MULTI_COUNTRY
    assert src_mod.normalise_country_tag("gb") == "GB"
    assert src_mod.normalise_country_tag(None) == ""
    # Not passed through. A tag that is not a country and is not one of the
    # two known non-country tags reads as a country to everything downstream.
    assert src_mod.normalise_country_tag("Europe") == "unknown"
    assert src_mod.normalise_country_tag("unknown") in src_mod.NON_COUNTRY_TAGS


def test_loading_a_source_file_normalises_the_tag_it_was_written_with():
    with _Tmp() as tmp:
        f = _source_file(tmp, [
            {"company": "Old spelling", "url": "https://a.example.test/jobs",
             "country": "multiple"},
            {"company": "Code", "url": "https://b.example.test/jobs",
             "country": "gb"},
        ])
        got = {s.company: s.country for s in src_mod.load_file(f)}
    assert got == {"Old spelling": src_mod.MULTI_COUNTRY, "Code": "GB"}


def test_the_contributor_notes_do_not_carry_a_rotting_number():
    """CLAUDE.md tells contributors not to write counts into prose, having
    shipped 17,625, 17,826, 17,828, "13 ATS APIs", "25 platforms" and "395
    tests" while none of them was true. It states the current ones anyway, for
    comments, so it has to be held to its own rule."""
    import json

    root = Path(__file__).resolve().parent.parent
    text = (root / "CLAUDE.md").read_text(encoding="utf-8")
    data = json.loads((root / "sources" / "sources.json").read_text(encoding="utf-8"))
    # The same definition sources.save() and meta.boards use: an employer's
    # own board, which excludes a cross-employer sweep as well as a keyword
    # template. This was the third test carrying the old rule, which is what
    # changing a definition costs.
    from jobradar.screen import directness
    boards = [s for s in data["sources"]
              if not s.get("keyword_template") and directness(s["platform"]) >= 2]

    from jobradar import adapters
    facts = {
        f"{len(boards):,} employer boards": True,
        # Wrapped across a line in CLAUDE.md, so the newline is normalised
        # away below like every other claim rather than skipped. This was
        # None with the note "checked below" and no check below it, so the
        # entries count read as verified and was not.
        f"{len(data['sources']):,} entries": True,
        f"{len(adapters.REGISTRY)} adapters": True,
        f"{len({s['platform'] for s in boards})} board platforms": True,
    }
    for claim, must in facts.items():
        if must is None:
            continue
        assert claim in text.replace("\n", " "), f"CLAUDE.md no longer says {claim!r}"
    # Written as an escape so this file does not itself contain the character
    # it is here to keep out, which is how test_workflows.py does it.
    assert "\u2014" not in text, "CLAUDE.md has an em-dash in it"
