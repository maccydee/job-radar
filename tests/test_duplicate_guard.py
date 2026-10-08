import contextlib
import io
import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import cli, runner, serve, store    # noqa: E402
from jobradar.models import Job                   # noqa: E402

CFG = ("titles:\n  include: ['engineering manager']\n"
       "locations:\n  countries: ['UK']\n"
       "sources:\n  use_bundled: false\n")
DESC = "We are hiring a leader for this team. " * 10


def _setup(roles):
    """`roles` is [(company, title, url)]; returns config, db and the uids."""
    d = Path(tempfile.mkdtemp())
    cfg = d / "config.yaml"
    cfg.write_text(CFG, encoding="utf-8")
    db = d / "t.db"
    con = store.connect(db)
    uids = []
    for company, title, url in roles:
        j = Job(company=company, title=title, url=url, platform="custom",
                location="London", description=DESC)
        store.upsert_roles(con, [j], run=1)
        uids.append(j.uid)
    con.close()
    return cfg, db, uids


def _brightwell():
    cfg, db, (li, jy) = _setup([
        ("Brightwell", "AI Lead", "https://www.linkedin.com/jobs/view/111"),
        ("Brightwell", "AI Lead", "https://jobs.jobylon.com/jobs/18640464")])
    con = store.connect(db)
    store.record_application(con, jy, applied_on="2026-09-27", route="jobylon",
                             reference="18640464")
    store.set_status(con, jy, "applied")
    con.close()
    return cfg, db, li, jy


@contextlib.contextmanager
def _stubbed_runner():
    """No process runs and no model is called: `claude_bin` answers and
    `run_job` is a recorder."""
    calls = []
    old = runner.claude_bin, runner.run_job
    runner.claude_bin = lambda: "claude"
    runner.run_job = lambda job_id, **kw: calls.append(job_id)
    try:
        yield calls
    finally:
        runner.claude_bin, runner.run_job = old


def _gen(cfg, db, target, *extra, kind="cv"):
    buf = io.StringIO()
    with _stubbed_runner() as calls, contextlib.redirect_stdout(buf):
        code = cli.main(["-c", str(cfg), "generate", target, "-k", kind, "--db", str(db), *extra])
    return code, buf.getvalue(), calls


def _jobs(db):
    return store.connect(db).execute("SELECT COUNT(*) FROM jobs").fetchone()[0]


def test_an_applied_role_is_refused_and_the_application_is_named():
    cfg, db, li, jy = _brightwell()
    code, out, calls = _gen(cfg, db, jy)
    assert code == 1 and calls == [] and _jobs(db) == 0, out
    assert ("Brightwell - AI Lead was already applied for on 2026-09-27 via jobylon "
            "(ref 18640464).") in out, out
    assert "Drafting a CV for it spends tokens for nothing." in out
    assert "--force" in out


def test_each_later_status_is_refused_and_named():
    for status in ("rejected", "withdrawn", "offer", "interviewing"):
        cfg, db, li, jy = _brightwell()
        con = store.connect(db)
        store.set_status(con, jy, status)
        con.close()
        code, out, calls = _gen(cfg, db, jy)
        assert code == 1 and calls == [], (status, out)
        assert status in out, (status, out)
        assert "2026-09-27" in out


def test_a_new_role_that_looks_like_an_applied_one_is_refused_naming_the_other():
    cfg, db, li, jy = _brightwell()
    code, out, calls = _gen(cfg, db, li)
    assert code == 1 and calls == [], out
    assert jy in out and "2026-09-27" in out and "jobylon" in out, out
    assert out.rstrip().endswith("If this is a different job, use --force."), out


def test_the_same_company_with_a_clearly_different_title_is_not_refused():
    cfg, db, (a, b) = _setup([
        ("Tessera", "Software Engineering Manager", "https://t.example/1"),
        ("Tessera", "Principal, AI Transformation", "https://t.example/2")])
    con = store.connect(db)
    store.record_application(con, b, applied_on="2026-10-01", route="ashby")
    store.set_status(con, b, "applied")
    con.close()
    code, out, calls = _gen(cfg, db, a)
    assert len(calls) == 1, out


def test_force_proceeds_and_still_prints_the_prior_application_once():
    cfg, db, li, jy = _brightwell()
    code, out, calls = _gen(cfg, db, jy, "--force")
    assert len(calls) == 1, out
    assert out.count("was already applied for on 2026-09-27") == 1, out


def test_a_screen_is_never_refused():
    cfg, db, li, jy = _brightwell()
    code, out, calls = _gen(cfg, db, jy, kind="screen")
    assert len(calls) == 1, out
    assert "already applied" not in out


def test_a_status_with_no_record_behind_it_is_still_refused_and_says_what_to_run():
    cfg, db, (uid,) = _setup([("Acme", "Engineering Manager", "https://a.example/1")])
    con = store.connect(db)
    store.set_status(con, uid, "applied")
    con.close()
    code, out, calls = _gen(cfg, db, uid)
    assert code == 1 and calls == [], out
    assert "applied" in out
    assert "no application record: run import-applications or applied --date" in out, out


def test_possible_duplicates_returns_the_other_role_and_not_itself():
    cfg, db, li, jy = _brightwell()
    con = store.connect(db)
    assert [d["uid"] for d in store.possible_duplicates(con, li)] == [jy]
    assert store.possible_duplicates(con, jy) == []
    own = store.prior_applications(con, jy)
    assert [p["uid"] for p in own] == [jy] and own[0]["same"] is True


def test_a_different_company_with_a_similar_name_is_not_the_same_employer():
    """`Application.matches` takes the company as a substring, so a role at
    Likewise Group matches an application made at Wise and a CV would be
    refused for a job nobody applied for."""
    cfg, db, (wise, likewise) = _setup([
        ("Wise", "Engineering Manager", "https://w.example/1"),
        ("Likewise Group", "Engineering Manager", "https://l.example/1")])
    con = store.connect(db)
    store.record_application(con, wise, applied_on="2026-10-01")
    store.set_status(con, wise, "applied")
    assert store.possible_duplicates(con, likewise) == []
    assert store.duplicate_refusal(con, likewise, "cv") is None


def test_applied_warns_about_the_same_job_under_another_uid():
    cfg, db, li, jy = _brightwell()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main(["-c", str(cfg), "applied", li, "-s", "applied", "--db", str(db)])
    out = buf.getvalue()
    assert code == 0, out
    assert f"WARNING: possibly the same job as {jy} (applied 2026-09-27 via jobylon)" in out, out


# --------------------------------------------------------------------- HTTP

@contextlib.contextmanager
def _server(db):
    serve.Handler.db_path = str(db)
    serve.Handler.docs_base = None
    serve.Handler.config_path = None
    old = runner.pump
    runner.pump = lambda **kw: None          # nothing is started
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        runner.pump = old
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def _post(base, path, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_the_generate_endpoint_refuses_an_applied_role_unless_forced():
    cfg, db, li, jy = _brightwell()
    with _server(db) as base:
        code, body = _post(base, "/api/generate", {"uid": jy, "kind": "cv"})
        assert code == 409, body
        assert "was already applied for on 2026-09-27 via jobylon" in body["error"], body
        assert _jobs(db) == 0
        code, body = _post(base, "/api/generate", {"uid": jy, "kind": "cv", "force": True})
        assert code == 200 and body["ok"] is True, body
        assert _jobs(db) == 1


def test_the_bulk_endpoint_says_which_roles_were_refused_for_being_applied():
    cfg, db, li, jy = _brightwell()
    with _server(db) as base:
        code, body = _post(base, "/api/generate/bulk", {"uids": [jy, li], "kind": "cv"})
        assert code == 200, body
        assert body["queued"] == [] and {s["uid"] for s in body["skipped"]} == {jy, li}, body
        code, body = _post(base, "/api/generate/bulk",
                           {"uids": [jy, li], "kind": "cv", "force": True})
        assert sorted(body["queued"]) == sorted([jy, li]), body


def test_the_endpoint_does_not_treat_a_truthy_string_as_force():
    cfg, db, li, jy = _brightwell()
    with _server(db) as base:
        code, body = _post(base, "/api/generate", {"uid": jy, "kind": "cv", "force": "false"})
        assert code == 409, body


# --------------------------------------- review finding 13: siblings and force

def _monarch_pair(a_title, b_title):
    con = store.connect(":memory:")
    uids = []
    for i, t in enumerate((a_title, b_title)):
        j = Job(company="Monarch", title=t, url=f"https://x/{i}", platform="custom", location="London")
        store.upsert_roles(con, [j], run=1)
        uids.append(j.uid)
    store.transition(con, uids[0], "rejected", source="cli", at="2026-05-01")
    return con, uids


def test_a_different_team_at_the_same_company_is_not_refused_as_the_same_job():
    con, (a, b) = _monarch_pair("Senior Engineering Manager, Payments",
                              "Senior Engineering Manager, Platform")
    assert store.duplicate_refusal(con, b, "cv") is None


def test_the_same_title_with_a_seniority_word_is_still_the_same_job():
    for a_title, b_title in (("Engineering Manager, Payments", "Senior Engineering Manager, Payments"),
                             ("Senior Engineering Manager", "Engineering Manager"),
                             ("AI Lead", "AI Lead")):
        con, (a, b) = _monarch_pair(a_title, b_title)
        assert store.duplicate_refusal(con, b, "cv") is not None, (a_title, b_title)


def test_the_same_role_is_still_refused():
    con, (a, b) = _monarch_pair("Senior Engineering Manager, Payments", "Head of Data")
    sentences, same = store.duplicate_refusal(con, a, "cv")
    assert same


def test_a_dashboard_refusal_says_it_is_a_duplicate_so_the_page_can_offer_draft_anyway():
    cfg, db, li, jy = _brightwell()
    with _server(db) as base:
        code, body = _post(base, "/api/generate", {"uid": jy, "kind": "cv"})
        assert code == 409 and body.get("duplicate") is True, body
        code, body = _post(base, "/api/generate", {"uid": "nope", "kind": "cv"})
        assert body.get("duplicate") is not True, body


def test_the_dashboard_page_has_a_draft_anyway_action_that_sends_force():
    from jobradar.output import interactive
    con = store.connect(":memory:")
    page = interactive.render(con)
    assert "Draft anyway" in page
    assert "force:true" in page.replace(" ", "")
