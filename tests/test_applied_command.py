import contextlib
import io
import json
import sys
import tempfile
import threading
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import cli, serve, store        # noqa: E402
from jobradar.models import Job               # noqa: E402

CFG = ("titles:\n  include: ['engineering manager']\n"
       "locations:\n  countries: ['UK']\n"
       "sources:\n  use_bundled: false\n")


def _setup(roles=(("Brightwell", "AI Lead"),)):
    d = Path(tempfile.mkdtemp())
    cfg = d / "config.yaml"
    cfg.write_text(CFG, encoding="utf-8")
    db = d / "t.db"
    con = store.connect(db)
    uids = []
    for i, (company, title) in enumerate(roles):
        j = Job(company=company, title=title, url=f"https://x.example/{i}",
                platform="custom", location="London")
        store.upsert_roles(con, [j], run=1)
        uids.append(j.uid)
    con.close()
    return cfg, db, uids


def _run(cfg, *argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main(["-c", str(cfg), *argv])
    return code, buf.getvalue()


def _con(db):
    return store.connect(db)


def test_applied_with_every_field_records_the_application():
    cfg, db, (uid,) = _setup()
    code, out = _run(cfg, "applied", "brightwell", "-s", "applied", "--date", "2026-09-27",
                     "--route", "jobylon", "--ref", "18640464", "--salary", "145000",
                     "--db", str(db))
    assert code == 0, out
    assert "recorded: applied 2026-09-27 via jobylon (ref 18640464)" in out, out
    con = _con(db)
    assert store.status_of(con, uid) == "applied"
    (a,) = store.applications_for(con, uid)
    assert (a["applied_on"], a["route"], a["reference"], a["salary_answer"], a["source"]) == (
        "2026-09-27", "jobylon", "18640464", "145000", "cli")


def test_a_later_status_keeps_the_applied_date():
    cfg, db, (uid,) = _setup()
    _run(cfg, "applied", uid, "--date", "2026-09-27", "--db", str(db))
    code, out = _run(cfg, "applied", uid, "-s", "rejected", "--db", str(db))
    assert code == 0, out
    con = _con(db)
    assert store.status_of(con, uid) == "rejected"
    assert store.applied_on(con, uid) == "2026-09-27"
    rej = [e for e in store.events_for(con, uid) if e["kind"] == "rejected"]
    assert [(e["at"], e["source"]) for e in rej] == [(date.today().isoformat(), "cli")]


def test_no_date_means_today_and_the_output_says_so():
    cfg, db, (uid,) = _setup()
    code, out = _run(cfg, "applied", uid, "-s", "applied", "--db", str(db))
    assert code == 0, out
    assert "date: today (pass --date to backdate)" in out, out
    assert store.applied_on(_con(db), uid) == date.today().isoformat()


def test_a_date_that_is_not_a_date_writes_nothing():
    cfg, db, (uid,) = _setup()
    code, out = _run(cfg, "applied", uid, "-s", "applied", "--date", "2026-13-40",
                     "--db", str(db))
    assert code == 1 and "not a date" in out, out
    con = _con(db)
    assert con.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM app_events").fetchone()[0] == 0
    assert store.status_of(con, uid) == "new"


def test_application_fields_only_apply_to_applied_or_submitted():
    cfg, db, (uid,) = _setup()
    code, out = _run(cfg, "applied", uid, "-s", "skipped", "--route", "x", "--db", str(db))
    assert code == 1, out
    assert "--route and the other application fields only apply to applied or submitted" in out
    assert store.status_of(_con(db), uid) == "new"


def test_closes_alone_sets_the_date_and_leaves_the_status():
    cfg, db, (uid,) = _setup()
    code, out = _run(cfg, "applied", uid, "--closes", "2026-10-09", "--db", str(db))
    assert code == 0, out
    con = _con(db)
    row = con.execute("SELECT closes_on, closes_evidence FROM roles WHERE uid=?", (uid,)).fetchone()
    assert (row["closes_on"], row["closes_evidence"]) == ("2026-10-09", "by hand")
    assert store.status_of(con, uid) == "new"
    assert con.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0


def test_a_bad_closes_date_writes_nothing_at_all():
    cfg, db, (uid,) = _setup()
    code, out = _run(cfg, "applied", uid, "-s", "applied", "--closes", "soon", "--db", str(db))
    assert code == 1 and "not a date" in out, out
    con = _con(db)
    assert store.status_of(con, uid) == "new"
    assert con.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0


def test_saying_applied_again_without_a_date_does_not_file_a_second_application():
    """The same trap as Brightwell, one level down: re-running the command on a
    later day would otherwise add an application dated that day."""
    cfg, db, (uid,) = _setup()
    _run(cfg, "applied", uid, "--date", "2026-09-27", "--route", "jobylon", "--db", str(db))
    code, out = _run(cfg, "applied", uid, "-s", "applied", "--note", "chased", "--db", str(db))
    assert code == 0, out
    assert "already recorded" in out and "2026-09-27" in out, out
    con = _con(db)
    assert len(store.applications_for(con, uid)) == 1
    assert [e["kind"] for e in store.events_for(con, uid)] == ["applied"]
    assert con.execute("SELECT note FROM role_state WHERE uid=?", (uid,)).fetchone()[0] == "chased"


def test_a_repeat_with_different_details_says_nothing_was_changed():
    # Review finding 7: a blank on file (the reference here) is now filled, and
    # a value that differs (the route) is still left alone and named.
    cfg, db, (uid,) = _setup()
    _run(cfg, "applied", uid, "--date", "2026-09-27", "--route", "jobylon", "--db", str(db))
    code, out = _run(cfg, "applied", uid, "--date", "2026-09-27", "--route", "linkedin",
                     "--ref", "99", "--db", str(db))
    assert code == 0, out
    assert "already on file" in out and "different route: not changed" in out, out
    (a,) = store.applications_for(_con(db), uid)
    assert a["route"] == "jobylon" and a["reference"] == "99"


def test_history_lists_each_event_with_its_source():
    cfg, db, (uid,) = _setup()
    _run(cfg, "applied", uid, "--date", "2026-09-27", "--route", "jobylon", "--ref", "18640464",
         "--db", str(db))
    code, out = _run(cfg, "history", uid, "--db", str(db))
    assert code == 0, out
    assert "2026-09-27  applied   jobylon 18640464  [cli]" in out, out


def test_history_of_a_role_with_no_history_says_so():
    cfg, db, (uid,) = _setup()
    code, out = _run(cfg, "history", uid, "--db", str(db))
    assert code == 0 and "no application history" in out, out


def test_a_dashboard_status_click_writes_an_event():
    cfg, db, (uid,) = _setup()
    serve.Handler.db_path = str(db)
    serve.Handler.docs_base = None
    serve.Handler.config_path = None
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_address[1]}/api/status",
            data=json.dumps({"uid": uid, "status": "applied"}).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            assert json.loads(r.read())["ok"] is True
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)
    ev = store.events_for(_con(db), uid)
    assert [(e["kind"], e["source"]) for e in ev] == [("applied", "dashboard")]


def test_a_dashboard_click_and_a_cli_record_on_one_day_are_one_event():
    con = store.connect(":memory:")
    j = Job(company="Acme", title="EM", url="https://a.example/1", platform="custom",
            location="London")
    store.upsert_roles(con, [j], run=1)
    store.transition(con, j.uid, "applied", source="dashboard")
    store.record_application(con, j.uid, source="cli")
    assert [e["kind"] for e in store.events_for(con, j.uid)] == ["applied"]


def test_applied_warns_when_another_role_looks_like_the_same_job():
    cfg, db, (first, second) = _setup([("Brightwell", "AI Lead"), ("Brightwell", "AI Lead")])
    _run(cfg, "applied", second, "--date", "2026-09-27", "--route", "jobylon", "--db", str(db))
    code, out = _run(cfg, "applied", first, "-s", "applied", "--db", str(db))
    assert code == 0, out
    assert f"WARNING: possibly the same job as {second} (applied 2026-09-27 via jobylon)" in out, out


# ------------------------------------------------- review finding 7: re-runs

def _rejected_with_application(cfg, db, uid):
    _run(cfg, "applied", uid, "--date", "2026-09-27", "--route", "jobylon", "--db", str(db))
    _run(cfg, "applied", uid, "-s", "rejected", "--db", str(db))
    assert store.status_of(_con(db), uid) == "rejected"


def test_saying_applied_again_never_moves_a_rejected_role_back_silently():
    cfg, db, (uid,) = _setup()
    _rejected_with_application(cfg, db, uid)
    for extra in ((), ("--contact", "Wren <wren@brightwell.com>")):
        code, out = _run(cfg, "applied", uid, *extra, "--db", str(db))
        assert code == 0, out
        assert store.status_of(_con(db), uid) == "rejected", out
        assert "status stays rejected" in out and "-> applied" not in out, out


def test_an_explicit_status_may_move_it_back_and_says_from_what():
    cfg, db, (uid,) = _setup()
    _rejected_with_application(cfg, db, uid)
    code, out = _run(cfg, "applied", uid, "-s", "applied", "--db", str(db))
    assert code == 0 and "rejected -> applied" in out, out
    assert store.status_of(_con(db), uid) == "applied"


def test_recording_a_first_application_on_a_rejected_role_keeps_the_status():
    """Same defect, other branch: no application row yet (a role rejected before
    the record existed), so `applied --date` files one, and must not reopen it."""
    cfg, db, (uid,) = _setup()
    _run(cfg, "applied", uid, "-s", "rejected", "--db", str(db))
    code, out = _run(cfg, "applied", uid, "--date", "2026-09-27", "--db", str(db))
    assert code == 0 and "recorded: applied 2026-09-27" in out, out
    assert store.status_of(_con(db), uid) == "rejected" and "status stays rejected" in out, out


def test_a_contact_can_be_added_to_an_existing_application_and_the_change_is_printed():
    cfg, db, (uid,) = _setup()
    _rejected_with_application(cfg, db, uid)
    code, out = _run(cfg, "applied", uid, "--contact", "Wren <wren@brightwell.com>", "--ref", "R1",
                     "--db", str(db))
    assert code == 0, out
    assert "updated: reference, contact" in out, out
    (a,) = store.applications_for(_con(db), uid)
    assert (a["contact"], a["reference"], a["route"]) == ("Wren <wren@brightwell.com>", "R1", "jobylon")


def test_a_different_value_is_only_overwritten_with_set():
    cfg, db, (uid,) = _setup()
    _run(cfg, "applied", uid, "--date", "2026-09-27", "--route", "jobylon", "--db", str(db))
    code, out = _run(cfg, "applied", uid, "--route", "linkedin", "--db", str(db))
    assert "different route" in out and "--set" in out, out
    assert store.applications_for(_con(db), uid)[0]["route"] == "jobylon"
    code, out = _run(cfg, "applied", uid, "--route", "linkedin", "--set", "--db", str(db))
    assert code == 0 and "updated: route" in out, out
    assert store.applications_for(_con(db), uid)[0]["route"] == "linkedin"


# ----------------------------------- review finding 20: dates for any status

def test_a_rejection_read_days_later_can_be_dated_the_day_it_came():
    cfg, db, (uid,) = _setup()
    _run(cfg, "applied", uid, "--date", "2026-09-27", "--db", str(db))
    code, out = _run(cfg, "applied", uid, "-s", "rejected", "--date", "2026-10-03", "--db", str(db))
    assert code == 0, out
    con = _con(db)
    assert store.status_of(con, uid) == "rejected"
    rej = [e for e in store.events_for(con, uid) if e["kind"] == "rejected"]
    assert [(e["at"], e["source"]) for e in rej] == [("2026-10-03", "cli")], rej
    assert len(store.applications_for(con, uid)) == 1          # no second application


def test_route_and_the_other_fields_still_only_apply_to_applied():
    cfg, db, (uid,) = _setup()
    code, out = _run(cfg, "applied", uid, "-s", "rejected", "--route", "x", "--db", str(db))
    assert code == 1 and "only apply to applied or submitted" in out, out
