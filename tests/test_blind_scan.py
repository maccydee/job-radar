"""A scan that could not read anything is not a scan that found nothing.

Until this existed the two were the same run from the outside. With 12 of 12
sources failing, the tool exited 0, overwrote a working dashboard with an
empty one (23,401 bytes down to 17,594, twelve roles down to none) and gave
the verdict:

    Nothing matched. Where they went:
    ...
    Most often this is the titles. Check `titles.include`

That blames the reader's config for their network. The proof was already on
the screen and unread: the "Where they went" breakdown printed empty, because
nothing had been screened at all. And the roles stayed in the database, so
`list` and `serve` then contradicted the dashboard the same run had written.

`ok` is how many sources answered, and it was already in scope on that line.
"""
import contextlib
import io
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar.cli import main  # noqa: E402


def _res(src, ok):
    """A real `fetch.Result`, so this test cannot drift from the real shape.

    A hand-rolled stub needed a new attribute every time `cmd_scan` grew one
    (`throttled`, then `truncated`), which is a test that fails for reasons
    unrelated to what it is testing.
    """
    from jobradar.fetch import Result
    return (Result(source=src, payload={"jobs": []}, status=200) if ok else
            Result(source=src, error="connection refused", status=None))


def _setup(tmp):
    cfg = tmp / "config.yaml"
    cfg.write_text(
        "titles:\n  include: [engineer]\n"
        "sources:\n  use_bundled: false\n"
        "  extra:\n"
        "    - {company: A, platform: greenhouse, url: 'https://a.invalid/x'}\n"
        "    - {company: B, platform: greenhouse, url: 'https://b.invalid/x'}\n"
        f"cv:\n  path: {cfg}\n", encoding="utf-8")
    return cfg


def _run(cfg, out, results):
    """`--state` into the temp tree, always. See test_cli_scan_reporting._scan:
    without it these tests write the developer's real state/seen.json."""
    buf = io.StringIO()
    state = Path(cfg).parent / "state" / "seen.json"
    with contextlib.redirect_stdout(buf), \
            mock.patch("jobradar.cli.fetch_all",
                       side_effect=lambda srcs, **k: [results(s) for s in srcs]):
        rc = main(["-c", str(cfg), "scan", "--no-enrich", "--no-caffeine",
                   "--no-open", "--db", ":memory:", "--out", str(out),
                   "--state", str(state)])
    return rc, buf.getvalue()


def test_every_source_failing_does_not_exit_zero():
    """So a cron job or a shell script notices, rather than logging success."""
    tmp = Path(tempfile.mkdtemp())
    rc, out = _run(_setup(tmp), tmp / "out", lambda s: _res(s, False))
    assert rc != 0, out


def test_it_says_it_could_not_look_rather_than_blaming_the_titles():
    tmp = Path(tempfile.mkdtemp())
    _, out = _run(_setup(tmp), tmp / "out", lambda s: _res(s, False))
    assert "could not look" in out
    assert "Check `titles.include`" not in out, \
        "a network failure was reported as a config problem"


def test_a_working_dashboard_is_not_replaced_with_an_empty_one():
    """The damage. Yesterday's roles are still worth reading; a blank page
    written over them is not, and cannot be undone."""
    tmp = Path(tempfile.mkdtemp())
    out = tmp / "out"
    out.mkdir()
    page = out / "index.html"
    page.write_text("<html>yesterday's roles</html>", encoding="utf-8")
    _run(_setup(tmp), out, lambda s: _res(s, False))
    assert page.read_text(encoding="utf-8") == "<html>yesterday's roles</html>"


def test_a_scan_that_read_boards_and_matched_nothing_still_says_so():
    """The other half. This must not turn every empty result into an error:
    a config too narrow for the market is a real answer and the diagnostic
    that goes with it is the useful part."""
    tmp = Path(tempfile.mkdtemp())
    rc, out = _run(_setup(tmp), tmp / "out", lambda s: _res(s, True))
    assert rc == 0, out
    assert "could not look" not in out
    assert "Nothing matched" in out
