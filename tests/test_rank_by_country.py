"""Ranking one country's roles without paying for everyone else's.

Asked for on 12 September 2026. The board held 828 UK roles and 4,272 American
ones for a reader searching the UK, and "Rank against my CV" priced and scored
all of them together, so the only way to rank the UK ones was to pay for both.

Two ways this could go wrong quietly, both tested:

  * The estimate and the run use different filters, so the confirm dialog
    shows the UK count and the background thread ranks the whole board.
  * A role with a blank country, or one open in several, is dropped from the
    count as if it were known to be elsewhere. It is left unranked and counted.

Nothing here calls a model: `_spawn_rank` is replaced before any request.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tempfile  # noqa: E402

from jobradar import rank, serve, store  # noqa: E402
from test_serve import _req, _server  # noqa: E402

DESC = "Requirements and responsibilities for this engineering role. " * 10
ROLES = [("uk1", "UK"), ("uk2", "UK"), ("us1", "US"), ("us2", "US"),
         ("us3", "US"), ("multi", "multiple"), ("blank", "")]


def _db(tmp):
    db = Path(tmp) / "board.db"
    con = store.connect(db)
    for uid, country in ROLES:
        con.execute(
            "INSERT INTO roles (uid,company,title,url,country,description,"
            "score,first_seen,last_seen) VALUES (?,?,?,?,?,?,1,"
            "date('now'),date('now'))",
            (uid, f"Co {uid}", "Engineering Manager",
             f"https://example.invalid/{uid}", country, DESC))
    con.commit()
    return db, con


def test_candidates_narrow_to_a_country():
    with tempfile.TemporaryDirectory() as tmp:
        db, con = _db(tmp)
        try:
            assert {r["uid"] for r in rank.candidates(con)} == {u for u, _ in ROLES}
            assert {r["uid"] for r in rank.candidates(con, countries=["UK"])} \
                == {"uk1", "uk2"}
            assert {r["uid"] for r in rank.candidates(con, countries=["unknown"])} \
                == {"blank"}
            # Blank and multi-country are counted, not silently dropped.
            assert rank.unplaced(con) == 2
        finally:
            con.close()


def test_the_cli_accepts_a_country_and_normalises_it():
    from jobradar.cli import build_parser
    args = build_parser().parse_args(["rank", "--country", "GB",
                                      "--country", "US", "--dry-run"])
    assert args.country == ["GB", "US"]


def test_the_dashboard_prices_and_ranks_the_same_country():
    spawned = {}
    real = serve._spawn_rank
    serve._spawn_rank = lambda *a, **k: spawned.update(k)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db, con = _db(tmp)
            con.close()
            with _server(db) as base:
                code, est = _req(base, "/api/rank?country=UK")
                assert code == 200, (code, est)
                assert est["pending"] == 2, est
                assert est["unplaced"] == 2, est

                code, all_est = _req(base, "/api/rank")
                assert all_est["pending"] == len(ROLES), all_est

                code, body = _req(base, "/api/rank", {"countries": ["UK"]})
                assert code == 200 and body["roles"] == 2, (code, body)
                assert spawned.get("countries") == ["UK"], \
                    "the run has to use the filter the click was priced with"

                c2 = store.connect(db)
                try:
                    store.release(c2, "rank")
                finally:
                    c2.close()
                code, body = _req(base, "/api/rank", {"countries": "UK"})
                assert code == 400, (code, body)
    finally:
        serve._spawn_rank = real


def test_the_rank_button_sends_the_selected_country():
    src = (Path(__file__).resolve().parent.parent / "jobradar" / "output"
           / "interactive.py").read_text(encoding="utf-8")
    assert "post('/api/rank',{countries:cs})" in src
    assert "'/api/rank'+(c.length?'?country='" in src
