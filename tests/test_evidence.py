import contextlib
import io
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import cli, store               # noqa: E402

TODAY = date(2026, 10, 8)


def test_add_returns_true_then_false_and_the_key_ignores_punctuation_and_spacing():
    con = store.connect(":memory:")
    assert store.add_evidence(con, "MARLOW & STONE", source="Glassdoor", figures="95-120k", url="u",
                              fetched_on="2026-10-08") is True
    assert store.add_evidence(con, "MARLOW & STONE", source="Glassdoor", figures="95-120k", url="u",
                              fetched_on="2026-10-08") is False
    for spelling in ("marlow&stone", "MARLOW & STONE", "marlow & stone", "MARLOW  &  STONE"):
        (row,) = store.evidence_for(con, spelling)
        assert row["company_key"] == "marlow stone" and row["fetched_on"] == "2026-10-08"


def test_the_same_figures_from_another_url_are_a_second_row():
    con = store.connect(":memory:")
    store.add_evidence(con, "Bluetree", source="Glassdoor", figures="90k", url="a", fetched_on="2026-10-08")
    assert store.add_evidence(con, "Bluetree", source="Glassdoor", figures="90k", url="b",
                              fetched_on="2026-10-08") is True
    assert len(store.evidence_for(con, "Bluetree")) == 2


def test_a_figure_with_no_source_is_not_evidence():
    con = store.connect(":memory:")
    for kw in ({"source": "", "figures": "90k"}, {"source": "Glassdoor", "figures": "  "}):
        try:
            store.add_evidence(con, "Bluetree", **kw)
        except ValueError as e:
            assert "a figure with no source is not evidence" in str(e)
            continue
        raise AssertionError(f"accepted {kw}")
    assert store.evidence_for(con, "Bluetree") == []


def test_a_company_with_no_letters_or_digits_is_refused():
    con = store.connect(":memory:")
    try:
        store.add_evidence(con, "&&&", source="x", figures="y")
    except ValueError as e:
        assert "company" in str(e)
        return
    raise AssertionError("an empty company key was stored")


def test_a_date_that_is_not_a_date_is_refused():
    con = store.connect(":memory:")
    try:
        store.add_evidence(con, "Bluetree", source="x", figures="y", fetched_on="last week")
    except ValueError:
        return
    raise AssertionError("a free-text date was stored")


def test_staleness_is_computed_at_the_boundary():
    row = {"source": "Glassdoor", "figures": "95-120k", "url": "", "fetched_on": "2026-06-10"}
    assert "STALE (120 days)" in store.describe_evidence(row, TODAY)
    fresh = dict(row, fetched_on="2026-09-28")
    assert "fresh (10 days)" in store.describe_evidence(fresh, TODAY)
    edge = dict(row, fetched_on="2026-07-10")                    # exactly 90 days
    assert "fresh (90 days)" in store.describe_evidence(edge, TODAY)
    assert "STALE (91 days)" in store.describe_evidence(dict(row, fetched_on="2026-07-09"), TODAY)


def test_a_url_is_shown_when_there_is_one():
    row = {"source": "Glassdoor", "figures": "95k", "url": "https://g.example/x", "fetched_on": "2026-10-01"}
    assert store.describe_evidence(row, TODAY).endswith("https://g.example/x")


def _db():
    d = Path(tempfile.mkdtemp())
    cfg = d / "config.yaml"
    cfg.write_text("titles:\n  include: ['engineering manager']\nlocations:\n  countries: ['UK']\n"
                   "sources:\n  use_bundled: false\n", encoding="utf-8")
    db = d / "t.db"
    store.connect(db).close()
    return cfg, db


def _run(cfg, *argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main(["-c", str(cfg), "evidence", *argv])
    return code, buf.getvalue()


def test_cli_add_prints_the_row_and_show_finds_it_by_any_spelling():
    cfg, db = _db()
    code, out = _run(cfg, "add", "MARLOW & STONE", "--source", "Glassdoor", "--url", "https://g.example/ms",
                     "--figures", "GBP 95-120k, Senior Manager", "--db", str(db))
    assert code == 0 and "Glassdoor: GBP 95-120k, Senior Manager" in out, out
    for spelling in ("marlow & stone", "Marlow&Stone"):
        code, out = _run(cfg, "show", spelling, "--db", str(db))
        assert code == 0 and "GBP 95-120k" in out and "fresh (0 days)" in out, out


def test_cli_add_without_a_source_or_figures_is_exit_1_and_writes_nothing():
    cfg, db = _db()
    code, out = _run(cfg, "add", "MARLOW & STONE", "--figures", "95k", "--db", str(db))
    assert code == 1 and "a figure with no source is not evidence" in out, out
    code, out = _run(cfg, "add", "MARLOW & STONE", "--source", "Glassdoor", "--db", str(db))
    assert code == 1 and "a figure with no source is not evidence" in out, out
    assert store.connect(db).execute("SELECT COUNT(*) FROM company_evidence").fetchone()[0] == 0


def test_cli_add_twice_the_same_day_says_it_is_already_on_file():
    cfg, db = _db()
    args = ("add", "Bluetree", "--source", "Glassdoor", "--figures", "90k", "--db", str(db))
    _run(cfg, *args)
    code, out = _run(cfg, *args)
    assert code == 0 and "already on file" in out, out
    assert store.connect(db).execute("SELECT COUNT(*) FROM company_evidence").fetchone()[0] == 1


def test_cli_show_of_an_unknown_company_states_the_absence():
    cfg, db = _db()
    code, out = _run(cfg, "show", "Unknown Co", "--db", str(db))
    assert code == 0 and "no evidence on file for 'Unknown Co'" in out, out


def test_cli_a_stale_row_says_stale():
    cfg, db = _db()
    con = store.connect(db)
    store.add_evidence(con, "Bluetree", source="Glassdoor", figures="90k", fetched_on="2026-01-01")
    con.close()
    code, out = _run(cfg, "show", "Bluetree", "--db", str(db))
    assert "STALE (" in out, out
