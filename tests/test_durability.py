"""What survives a process being killed part way through a write.

Every test here kills a write for real: the file object is swapped for one
that puts the first half of the bytes on disk and then raises, which is what a
GitHub Actions job timeout, a Ctrl-C or an OOM kill look like from inside the
process. The assertions are about what is left on disk afterwards, not about
how the writer is spelled.

The case that prompted this: `State.save` was a plain `write_text`, which
truncates the target and then streams into it. A scan killed at the Actions
timeout left a truncated seen.json; `State.load` swallowed the JSONDecodeError
and returned an empty state; every role then looked new, and the next save
overwrote the file with that empty answer, which the workflow committed. The
corruption was silent in both directions.
"""

from __future__ import annotations

import builtins
import io
import json
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import docx, setup_wizard, sources as src_mod
from jobradar import output
from jobradar.models import Source
from jobradar.output import html as html_out
from jobradar.state import (State, StateUnreadable, atomic_write_bytes,
                            atomic_write_text)


# ------------------------------------------------------------------ killing


class _Killed(BaseException):
    """Not an Exception, on purpose.

    A killed process does not raise something an `except Exception` will catch,
    and the cleanup in the writers has to run anyway. If this test suite passes
    with `except Exception` in the writers, the test is not testing anything.
    """


class _DiesHalfway:
    """A file that writes the first half of what it is given, then dies."""

    def __init__(self, f):
        self._f = f

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._f.close()
        return False

    def write(self, data):
        half = len(data) // 2
        self._f.write(data[:half])
        self._f.flush()
        raise _Killed("killed mid-write")

    def flush(self):
        self._f.flush()

    def fileno(self):
        return self._f.fileno()

    def close(self):
        self._f.close()


class kill_writes_under:
    """Kill every write opened for writing inside `root`, half way through.

    Three names are swapped, because one file can be opened by three routes.
    The writers in this package call the builtin. `Path.write_text` goes
    through `io.open` on 3.11 and later, and on 3.10 and earlier it goes
    through `Path.open`, which binds `io.open` when the class is defined, so
    a patch applied afterwards can never reach it. Patching `Path.open` as
    well covers both, and covers any future rearrangement of pathlib's
    internals, because it is the public name either way.

    That is not a hypothetical. The first version of this harness patched two
    of the three, and the meta-test below caught it on 3.10 and only on 3.10:
    the plain write it is supposed to destroy came through untouched. Which is
    the whole reason the meta-test exists.
    """

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self._real = builtins.open
        self._real_io = io.open
        self._real_path_open = Path.open

    def _patched(self, file, mode="r", *a, **kw):
        f = self._real(file, mode, *a, **kw)
        try:
            inside = Path(file).resolve().is_relative_to(self.root)
        except (TypeError, ValueError, AttributeError, OSError):
            # is_relative_to arrived in 3.9; fall back to a string compare so
            # this file does not decide the minimum Python version.
            try:
                inside = str(Path(file).resolve()).startswith(str(self.root))
            except (TypeError, ValueError, OSError):
                inside = False
        if inside and "w" in str(mode):
            return _DiesHalfway(f)
        return f

    def _patched_path_open(self, path_self, mode="r", *a, **kw):
        return self._patched(path_self, mode, *a, **kw)

    def __enter__(self):
        builtins.open = self._patched
        io.open = self._patched
        Path.open = lambda s_, mode="r", *a, **kw: self._patched(s_, mode, *a, **kw)
        return self

    def __exit__(self, *exc):
        builtins.open = self._real
        io.open = self._real_io
        Path.open = self._real_path_open
        return False


def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="jr-durability-"))


def _strays(d: Path) -> list[str]:
    """Temp files the writers left behind. There should never be any."""
    return sorted(p.name for p in Path(d).iterdir() if ".tmp" in p.name)


GOOD_STATE = {
    "runs": 4,
    "updated": "2026-08-20",
    "seen": {f"uid{i}": {"first_seen": "2026-08-01", "last_seen": "2026-08-20",
                         "company": "Acme", "title": "Engineering Manager"}
             for i in range(200)},
    "source_counts": {"greenhouse": 41},
}


def _write_good_state(d: Path) -> Path:
    p = d / "seen.json"
    p.write_text(json.dumps(GOOD_STATE, indent=1, sort_keys=True),
                 encoding="utf-8")
    return p


# ------------------------------------------------------- the helper itself


def test_an_interrupted_atomic_write_leaves_the_old_file_byte_for_byte():
    d = _tmpdir()
    p = d / "thing.txt"
    p.write_text("the good copy\n", encoding="utf-8")
    before = p.read_bytes()

    killed = False
    try:
        with kill_writes_under(d):
            atomic_write_text(p, "x" * 4000)
    except _Killed:
        killed = True

    assert killed, "the test did not actually interrupt anything"
    assert p.read_bytes() == before, "the old file was damaged"
    assert _strays(d) == [], f"a half-written temp file was left: {_strays(d)}"


def test_an_interrupted_first_write_creates_nothing_at_all():
    """A partial file at a path that had none is worse than no file: the next
    run finds something there and reads it."""
    d = _tmpdir()
    p = d / "new.json"
    try:
        with kill_writes_under(d):
            atomic_write_text(p, json.dumps({"a": 1}) * 500)
    except _Killed:
        pass
    assert not p.exists(), "an interrupted write created a partial file"
    assert _strays(d) == []


def test_the_temp_file_sits_in_the_target_directory():
    """A rename is only atomic within one filesystem, so the temp file cannot
    live in /tmp. Caught by writing while watching the directory."""
    d = _tmpdir()
    p = d / "sub" / "deep.txt"
    seen: list[str] = []
    real = builtins.open

    def watch(file, mode="r", *a, **kw):
        if "w" in str(mode):
            seen.append(str(file))
        return real(file, mode, *a, **kw)

    builtins.open = watch
    try:
        atomic_write_text(p, "hello")
    finally:
        builtins.open = real

    assert p.read_text(encoding="utf-8") == "hello"
    assert seen and Path(seen[0]).parent == p.parent, \
        f"wrote through {seen} rather than through the target directory"


def test_replacing_an_existing_file_is_allowed():
    """os.rename raises FileExistsError on Windows when the target exists, and
    CI runs Windows. os.replace does not. This is the test that fails there if
    anybody swaps them back."""
    d = _tmpdir()
    p = d / "twice.txt"
    atomic_write_text(p, "first")
    atomic_write_text(p, "second")
    assert p.read_text(encoding="utf-8") == "second"
    assert _strays(d) == []


def test_atomic_write_bytes_round_trips_and_replaces():
    d = _tmpdir()
    p = d / "blob.bin"
    atomic_write_bytes(p, b"\x00\x01old")
    atomic_write_bytes(p, b"\x00\x01new")
    assert p.read_bytes() == b"\x00\x01new"


# ------------------------------------------------------------- the seen-set


def test_a_scan_killed_mid_save_does_not_lose_the_seen_set():
    """The known case. The file has to still hold 200 uids afterwards."""
    d = _tmpdir()
    p = _write_good_state(d)

    st = State(p)
    assert len(st.seen) == 200
    st.seen["uid-new"] = {"first_seen": "2026-08-24", "last_seen": "2026-08-24",
                          "company": "New", "title": "EM"}

    try:
        with kill_writes_under(d):
            st.save()
    except _Killed:
        pass

    reread = State(p)
    assert len(reread.seen) == 200, "the seen-set was lost by an interrupted save"
    assert reread.runs == 4
    assert reread.source_counts == {"greenhouse": 41}
    assert _strays(d) == []


def test_a_truncated_seen_file_is_reported_rather_than_read_as_empty():
    """This is the half that made the old bug silent. A file that will not
    parse must not come back as "nothing has ever been seen"."""
    d = _tmpdir()
    p = _write_good_state(d)
    whole = p.read_text(encoding="utf-8")
    p.write_text(whole[:len(whole) // 2], encoding="utf-8")   # a killed write

    try:
        State(p)
    except StateUnreadable as e:
        msg = str(e)
        assert str(p) in msg, "the message has to name the file"
        assert "Refusing" in msg
        assert "every role as new" in msg, \
            "the message has to say what the old behaviour would have done"
    else:
        raise AssertionError(
            "a truncated seen.json was read as an empty state, which is the "
            "bug: every role looks new and the file is then overwritten")


def test_the_unreadable_file_is_left_exactly_as_it_was():
    """Refusing is only worth anything if the bytes survive to be recovered."""
    d = _tmpdir()
    p = _write_good_state(d)
    whole = p.read_text(encoding="utf-8")
    p.write_text(whole[:400], encoding="utf-8")
    before = p.read_bytes()

    try:
        State(p)
    except StateUnreadable:
        pass
    assert p.read_bytes() == before, "the refusal damaged the file it refused"
    assert _strays(d) == []


def test_an_empty_seen_file_is_unreadable_not_empty():
    """Truncation to zero bytes is the commonest shape of all, and json.loads
    on it raises rather than returning {}."""
    d = _tmpdir()
    p = d / "seen.json"
    p.write_text("", encoding="utf-8")
    try:
        State(p)
    except StateUnreadable:
        return
    raise AssertionError("a zero-byte seen.json was treated as an empty state")


def test_a_seen_file_holding_the_wrong_shape_is_reported_not_crashed():
    """Valid JSON that is not an object used to reach `d.get` and die with an
    AttributeError, which says nothing about what is wrong or what to do."""
    d = _tmpdir()
    p = d / "seen.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    try:
        State(p)
    except StateUnreadable as e:
        assert "not an object" in str(e)
        return
    raise AssertionError("a JSON list was accepted as a seen-set")


def test_a_missing_seen_file_is_still_a_clean_start():
    """The refusal must not turn a first run into an error."""
    d = _tmpdir()
    st = State(d / "nothing-here.json")
    assert st.seen == {} and st.runs == 0


def test_state_round_trips_through_the_atomic_save():
    d = _tmpdir()
    p = d / "state" / "seen.json"          # a directory that does not exist yet
    st = State(p)
    st.seen = {"a": {"first_seen": "2026-08-01", "last_seen": "2026-08-01",
                     "company": "Acme", "title": "EM"}}
    st.source_counts = {"lever": 3}
    st.runs = 2
    st.save()
    assert State(p).seen == st.seen
    assert State(p).runs == 2
    assert _strays(p.parent) == []


# ---------------------------------------------------------- the source list


def _source_file(d: Path) -> Path:
    p = d / "sources.json"
    src_mod.save(
        [Source(company=f"C{i}", url=f"https://boards.greenhouse.io/c{i}",
                platform="greenhouse") for i in range(50)],
        p, meta={"note": "harvested by hand", "version": 4})
    return p


def test_an_interrupted_prune_does_not_destroy_the_source_list():
    """`validate --prune` rewrites 17,810 entries in place every Sunday, and
    nothing in this repository can rebuild that list: the crawler that finds
    employers runs upstream."""
    d = _tmpdir()
    p = _source_file(d)
    before = p.read_bytes()

    keep = [Source(company="C0", url="https://boards.greenhouse.io/c0",
                   platform="greenhouse")]
    try:
        with kill_writes_under(d):
            src_mod.save(keep, p, meta={"pruned": 49})
    except _Killed:
        pass

    body = json.loads(p.read_text(encoding="utf-8"))
    assert len(body["sources"]) == 50, "an interrupted prune ate the list"
    assert body["meta"]["note"] == "harvested by hand"
    assert p.read_bytes() == before
    assert _strays(d) == []


def test_the_source_list_still_merges_its_metadata_after_the_change():
    d = _tmpdir()
    p = _source_file(d)
    src_mod.save([], p, meta={"pruned": 3})
    meta = json.loads(p.read_text(encoding="utf-8"))["meta"]
    assert meta["note"] == "harvested by hand" and meta["version"] == 4
    assert meta["pruned"] == 3 and meta["boards"] == 0


# --------------------------------------------------------------- the config


def test_an_interrupted_setup_does_not_shred_an_existing_config():
    """`setup` rewrites the config in place. Half a config is not something
    the user can recover from anything this tool holds."""
    d = _tmpdir()
    p = d / "config.yaml"
    setup_wizard.write_config(p, {"titles_include": ["engineering manager"],
                                  "countries": ["UK"], "cv_path": "CV.md"})
    before = p.read_bytes()
    assert b"engineering manager" in before

    try:
        with kill_writes_under(d):
            setup_wizard.write_config(p, {"titles_include": ["nurse"],
                                          "countries": ["UK"]})
    except _Killed:
        pass

    assert p.read_bytes() == before, "an interrupted setup damaged the config"
    assert _strays(d) == []


# ------------------------------------------------------------ the .docx


DOC_MD = "# Rowan Ashby\n\nEngineering manager\n\n## Profile\n\nSix engineers.\n"


def test_an_interrupted_docx_leaves_the_previous_one_openable():
    """A zip written straight to the final path leaves a file with a .docx
    name that no word processor will open, where a good one used to be."""
    d = _tmpdir()
    p = d / "CV.docx"
    docx.markdown_to_docx(DOC_MD, p)
    assert "Rowan Ashby" in zipfile.ZipFile(p).read(
        "word/document.xml").decode("utf-8")

    try:
        with kill_writes_under(d):
            docx.markdown_to_docx("# Someone Else\n\nRewritten.\n", p)
    except _Killed:
        pass

    # The proof is that it still opens, not that the bytes match.
    doc = zipfile.ZipFile(p).read("word/document.xml").decode("utf-8")
    assert "Rowan Ashby" in doc, "the interrupted write ate the old document"
    assert _strays(d) == []


def test_a_docx_written_normally_is_still_a_valid_zip():
    d = _tmpdir()
    p = d / "sub" / "CV.docx"
    docx.markdown_to_docx(DOC_MD, p)
    z = zipfile.ZipFile(p)
    assert z.testzip() is None
    # The five document parts must all be there. docProps/ is allowed on top:
    # it is the author and title, tested in test_docx_properties.py, and an
    # exact-set check here turned adding it into a durability failure.
    assert {
        "[Content_Types].xml", "_rels/.rels", "word/_rels/document.xml.rels",
        "word/styles.xml", "word/document.xml"} <= set(z.namelist())


# ------------------------------------------------------------- the outputs


def test_an_interrupted_dashboard_leaves_yesterdays_page():
    """A browser renders a truncated page and shows no error, so half a
    dashboard looks like a dashboard with half the roles in it."""
    d = _tmpdir()
    p = d / "index.html"
    html_out.write(p, new=[], seen=[], dropped={}, sources_ok=3,
                   sources_total=3, throttled=[], postings=9)
    before = p.read_bytes()
    assert before.rstrip().endswith(b"</html>")

    try:
        with kill_writes_under(d):
            html_out.write(p, new=[], seen=[], dropped={}, sources_ok=1,
                           sources_total=3, throttled=[], postings=1)
    except _Killed:
        pass

    assert p.read_bytes() == before
    assert p.read_bytes().rstrip().endswith(b"</html>")
    assert _strays(d) == []


def test_an_interrupted_roles_json_stays_parseable():
    """out/roles.json is regenerable, but only by another full scan over the
    network, and half of it is a parse error for everything downstream."""
    d = _tmpdir()
    p = d / "roles.json"
    output.write_json(p, [], [], {"sources_ok": 3, "sources_total": 3})
    before = p.read_bytes()

    try:
        with kill_writes_under(d):
            output.write_json(p, [], [], {"sources_ok": 1, "sources_total": 3})
    except _Killed:
        pass

    assert json.loads(p.read_text(encoding="utf-8"))["meta"]["sources_ok"] == 3
    assert p.read_bytes() == before
    assert _strays(d) == []


def test_the_markdown_report_is_atomic_too():
    d = _tmpdir()
    p = d / "roles.md"
    output.write_markdown(p, [], [], {"sources_ok": 3, "sources_total": 3})
    before = p.read_bytes()
    try:
        with kill_writes_under(d):
            output.write_markdown(p, [], [], {"sources_ok": 1,
                                              "sources_total": 3})
    except _Killed:
        pass
    assert p.read_bytes() == before
    assert _strays(d) == []


def test_the_output_writers_still_return_the_path_they_wrote():
    """cmd_scan prints these back, so returning None would print "wrote None"."""
    d = _tmpdir()
    assert output.write_json(d / "roles.json", [], [], {}) == d / "roles.json"
    assert output.write_markdown(d / "roles.md", [], [], {}) == d / "roles.md"
    assert html_out.write(d / "i.html", new=[], seen=[], dropped={},
                          sources_ok=1, sources_total=1,
                          throttled=[]) == d / "i.html"
    assert docx.markdown_to_docx(DOC_MD, d / "cv.docx") == d / "cv.docx"


# ------------------------------------------------- what was left as it was


def test_the_favicon_is_still_a_plain_write_and_that_is_the_decision():
    """Judged safe rather than fixed: a module constant of a few hundred
    bytes, reproduced exactly by calling the function again. The test is here
    so the decision is recorded rather than looking like an oversight."""
    from jobradar.output import favicon
    d = _tmpdir()
    p = d / "favicon.svg"
    favicon.write(p)
    assert p.read_text(encoding="utf-8") == favicon.SVG
    favicon.write(p)
    assert p.read_text(encoding="utf-8") == favicon.SVG


def _self_check() -> None:
    """The kill harness has to be able to break a plain write, or every test
    above passes for the wrong reason."""
    d = _tmpdir()
    p = d / "plain.txt"
    p.write_text("the good copy, long enough to be worth truncating\n",
                 encoding="utf-8")
    try:
        with kill_writes_under(d):
            p.write_text("x" * 2000, encoding="utf-8")
    except _Killed:
        pass
    assert p.read_text(encoding="utf-8") != "the good copy, long enough to be " \
        "worth truncating\n", "the harness did not interrupt a plain write"
    assert len(p.read_text(encoding="utf-8")) < 2000


def test_the_interruption_harness_really_interrupts():
    _self_check()


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  pass  {name}")
            except BaseException as e:                 # noqa: BLE001
                fails += 1
                print(f"  FAIL  {name}: {e!r}")
    raise SystemExit(1 if fails else 0)
