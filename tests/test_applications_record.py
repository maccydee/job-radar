import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import store                    # noqa: E402
from jobradar.models import Job               # noqa: E402


def _con_with_role(company="Brightwell", title="AI Lead"):
    con = store.connect(":memory:")
    j = Job(company=company, title=title, url="https://x.example/job/1",
            platform="linkedin", location="London")
    store.upsert_roles(con, [j], run=1)
    return con, j.uid


def test_record_application_writes_row_and_event_once():
    con, uid = _con_with_role()
    assert store.record_application(con, uid, applied_on="2026-09-27",
                                    route="jobylon", reference="18640464") is True
    assert store.record_application(con, uid, applied_on="2026-09-27",
                                    route="jobylon", reference="18640464") is False
    assert con.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 1
    ev = con.execute("SELECT kind, at FROM app_events").fetchall()
    assert [(e["kind"], e["at"]) for e in ev] == [("applied", "2026-09-27")]


def test_applied_date_survives_later_status_changes():
    """role_state.updated_at is overwritten by every change; the record must not be."""
    con, uid = _con_with_role()
    store.record_application(con, uid, applied_on="2026-09-27")
    store.transition(con, uid, "interviewing", source="manual")
    store.transition(con, uid, "rejected", note="form letter", source="mail:abc")
    assert store.applied_on(con, uid) == "2026-09-27"
    kinds = [r["kind"] for r in store.events_for(con, uid)]
    assert kinds == ["applied", "interviewing", "rejected"]


def test_transition_writes_the_event_with_its_source_and_status():
    con, uid = _con_with_role()
    store.transition(con, uid, "rejected", note="form letter", source="mail:abc")
    assert store.status_of(con, uid) == "rejected"
    ev = store.events_for(con, uid)
    assert [(e["kind"], e["source"], e["detail"]) for e in ev] == [
        ("rejected", "mail:abc", "form letter")]


def test_a_pre_change_database_upgrades_in_place():
    d = Path(tempfile.mkdtemp())
    p = d / "old.db"
    raw = sqlite3.connect(p)
    raw.executescript("""
        CREATE TABLE roles (uid TEXT PRIMARY KEY, company TEXT NOT NULL DEFAULT '',
          title TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '',
          first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
        CREATE TABLE role_state (uid TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'new',
          note TEXT DEFAULT '', updated_at TEXT NOT NULL);
        INSERT INTO roles VALUES ('u1','Acme','EM','https://a/1','2026-09-01','2026-09-02');
    """)
    raw.commit()
    raw.close()
    con = store.connect(p)
    assert con.execute("SELECT company FROM roles WHERE uid='u1'").fetchone()[0] == "Acme"
    assert {r["name"] for r in con.execute("PRAGMA table_info(roles)")} >= {"closes_on", "closes_evidence"}
    store.record_application(con, "u1", applied_on="2026-09-30")   # tables exist
    con.close()


def test_merging_two_roles_keeps_the_application_history():
    con, keep = _con_with_role()
    lose_job = Job(company="Brightwell", title="AI Lead", url="https://jobylon.example/9",
                   platform="jobylon", location="London")
    store.upsert_roles(con, [lose_job], run=1)
    lose = lose_job.uid
    store.record_application(con, lose, applied_on="2026-09-27", route="jobylon")
    store._absorb_into(con, keep=keep, lose=lose)
    assert con.execute("SELECT COUNT(*) FROM roles WHERE uid=?", (lose,)).fetchone()[0] == 0
    assert store.applied_on(con, keep) == "2026-09-27"
    assert con.execute("SELECT COUNT(*) FROM applications WHERE uid=?", (keep,)).fetchone()[0] == 1
    assert [e["kind"] for e in store.events_for(con, keep)] == ["applied"]


def test_absorb_does_not_swallow_a_missing_table():
    con, keep = _con_with_role()
    con.execute("DROP TABLE app_events")
    try:
        store._absorb_into(con, keep=keep, lose="nope")
    except sqlite3.OperationalError:
        return
    raise AssertionError("a missing table was silently skipped")


def test_merge_duplicates_moves_the_application_instead_of_cascading_it_away():
    """merge_duplicates deletes the losing roles row, and applications and
    app_events reference roles ON DELETE CASCADE: without a move, the one row
    recording that an application went in disappears with the duplicate."""
    con = store.connect(":memory:")
    a = Job(company="Brightwell", title="AI Lead", url="https://www.linkedin.com/jobs/view/1",
            platform="linkedin", location="London")
    b = Job(company="Brightwell", title="AI Lead", url="https://jobs.jobylon.com/jobs/9",
            platform="jobylon", location="London")
    store.upsert_roles(con, [a, b], run=1)
    # The Jobylon posting is the employer's own board, so it survives; the
    # application was made through the LinkedIn copy, which is the loser.
    store.record_application(con, a.uid, applied_on="2026-10-06", route="LinkedIn")
    assert store.merge_duplicates(con) == 1
    survivors = [r["uid"] for r in con.execute("SELECT uid FROM roles")]
    assert len(survivors) == 1
    assert store.applied_on(con, survivors[0]) == "2026-10-06"
    assert [e["kind"] for e in store.events_for(con, survivors[0])] == ["applied"]


def test_rekeying_a_role_moves_its_application_with_it():
    """rekey_uids renames roles.uid with foreign keys deferred to the commit;
    a child table it forgets leaves the commit refusing, and the rename with it."""
    con = store.connect(":memory:")
    j = Job(company="Acme", title="EM", url="https://x.example/job/1",
            platform="custom", location="London")
    store.upsert_roles(con, [j], run=1)
    store.record_application(con, j.uid, applied_on="2026-10-01")
    # Give the row an id the current rule would not derive, as an old database has.
    con.execute("PRAGMA foreign_keys=OFF")
    for t in ("role_state", "artifacts", "jobs", "applications", "app_events"):
        con.execute(f"UPDATE {t} SET uid='old-id' WHERE uid=?", (j.uid,))
    con.execute("UPDATE roles SET uid='old-id' WHERE uid=?", (j.uid,))
    con.execute("PRAGMA foreign_keys=ON")
    assert store.rekey_uids(con) == 1
    assert store.applied_on(con, j.uid) == "2026-10-01"
    assert con.execute("SELECT COUNT(*) FROM app_events WHERE uid=?", (j.uid,)).fetchone()[0] == 1
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []


def test_rescreen_remove_never_deletes_a_role_that_has_an_application():
    """`rescreen --remove` deletes roles that no longer match and have no
    status; the application tables cascade, so a role with an application
    record but no status would take it with it."""
    import contextlib
    import io
    from jobradar import cli
    d = Path(tempfile.mkdtemp())
    cfg = d / "config.yaml"
    cfg.write_text("titles:\n  include: ['engineering manager']\n"
                   "locations:\n  countries: ['UK']\n"
                   "sources:\n  use_bundled: false\n", encoding="utf-8")
    db = d / "t.db"
    con = store.connect(db)
    for uid, title in (("u-plain", "Plumber"), ("u-applied", "Plumber")):
        con.execute(
            "INSERT INTO roles (uid,company,title,url,location,platform,"
            "description,first_seen,last_seen) VALUES (?,?,?,?,?,?,?,?,?)",
            (uid, "Zzz", title, f"https://x.example/{uid}", "Manchester, United Kingdom",
             "ashby", "pipes", "2026-10-01", "2026-10-01"))
    store.record_application(con, "u-applied", applied_on="2026-10-01")
    con.close()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main(["-c", str(cfg), "rescreen", "--db", str(db), "--remove"])
    assert code == 0, buf.getvalue()
    con = store.connect(db)
    left = {r["uid"] for r in con.execute("SELECT uid FROM roles")}
    assert left == {"u-applied"}, (left, buf.getvalue())
    assert store.applied_on(con, "u-applied") == "2026-10-01"


def test_the_applications_tables_exist_on_a_fresh_database():
    con = store.connect(":memory:")
    names = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"applications", "app_events", "mail_proposals", "company_evidence"} <= names


# ------------------------------------------- review findings 8 and 9: merges

def _pair(con):
    a = Job(company="Wise", title="Engineering Manager", url="https://www.linkedin.com/jobs/view/1",
            platform="linkedin", location="London")
    b = Job(company="Wise", title="Engineering Manager", url="https://jobs.smartrecruiters.com/Wise/2",
            platform="smartrecruiters", location="London")
    store.upsert_roles(con, [a, b], run=1)
    con.execute("UPDATE roles SET description=? WHERE uid=?", ("x" * 500, b.uid))
    return a.uid, b.uid           # b is the employer's board: it survives


def _propose_rejection(con):
    from jobradar import mailsync
    mailsync.propose(con, {"folders_read": ["inbox", "deleteditems", "junkemail"], "messages": [
        {"id": "m1", "folder": "inbox", "received": "2026-10-07T09:00:00Z",
         "subject": "Wise - Engineering Manager", "sender": "x@wise.com",
         "body": "We regret to inform you that we will not be progressing."}]})


def test_a_merge_re_points_pending_mail_proposals_to_the_surviving_role():
    from jobradar import mailsync
    con = store.connect(":memory:")
    a, b = _pair(con)
    con.execute("DELETE FROM roles WHERE uid=?", (b,))          # only the LinkedIn copy at first
    store.record_application(con, a, applied_on="2026-10-01", source="cli")
    store.transition(con, a, "applied", source="cli", at="2026-10-01")
    _propose_rejection(con)
    (p,) = mailsync.list_proposals(con)
    assert p["uid"] == a
    _, b = _pair(con)
    assert store.merge_duplicates(con) == 1
    (p,) = mailsync.list_proposals(con)
    assert p["uid"] == b, p
    out = mailsync.apply_proposals(con, [p["id"]])
    assert out["applied"] == [p["id"]], out
    assert store.status_of(con, b) == "rejected"


def test_a_rekey_re_points_pending_mail_proposals():
    from jobradar import mailsync
    con = store.connect(":memory:")
    j = Job(company="Acme", title="EM", url="https://x.example/job/1", platform="custom",
            location="London")
    store.upsert_roles(con, [j], run=1)
    store.record_application(con, j.uid, applied_on="2026-10-01")
    store.set_status(con, j.uid, "applied")
    mailsync.propose(con, {"folders_read": ["inbox", "deleteditems", "junkemail"], "messages": [
        {"id": "m1", "folder": "inbox", "received": "2026-10-07T09:00:00Z", "subject": "Acme",
         "sender": "x@acme.com", "body": "We regret to inform you that we will not be progressing."}]})
    con.execute("PRAGMA foreign_keys=OFF")
    for t in ("role_state", "artifacts", "jobs", "applications", "app_events", "mail_proposals"):
        con.execute(f"UPDATE {t} SET uid='old-id' WHERE uid=?", (j.uid,))
    con.execute("UPDATE roles SET uid='old-id' WHERE uid=?", (j.uid,))
    con.execute("PRAGMA foreign_keys=ON")
    assert store.rekey_uids(con) == 1
    assert [p["uid"] for p in mailsync.list_proposals(con)] == [j.uid]


def test_rescreen_remove_never_deletes_a_role_with_mail_or_events_about_it():
    """A pending proposal or an `acknowledged` event is a record that something
    happened with the role, and deleting the role strands the one and cascades
    the other away."""
    import contextlib
    import io
    from jobradar import cli
    d = Path(tempfile.mkdtemp())
    cfg = d / "config.yaml"
    cfg.write_text("titles:\n  include: ['engineering manager']\n"
                   "locations:\n  countries: ['UK']\n"
                   "sources:\n  use_bundled: false\n", encoding="utf-8")
    db = d / "t.db"
    con = store.connect(db)
    for uid in ("u-plain", "u-event", "u-mail"):
        con.execute(
            "INSERT INTO roles (uid,company,title,url,location,platform,"
            "description,first_seen,last_seen) VALUES (?,?,?,?,?,?,?,?,?)",
            (uid, "Zzz", "Plumber", f"https://x.example/{uid}", "Manchester, United Kingdom",
             "ashby", "pipes", "2026-10-01", "2026-10-01"))
    store.add_event(con, "u-event", "acknowledged", at="2026-10-01", source="mail:k")
    con.execute("INSERT INTO mail_proposals (message_id, kind, uid, created_at) "
                "VALUES ('m9','rejection','u-mail','2026-10-01')")
    con.close()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main(["-c", str(cfg), "rescreen", "--db", str(db), "--remove"])
    assert code == 0, buf.getvalue()
    con = store.connect(db)
    left = {r["uid"] for r in con.execute("SELECT uid FROM roles")}
    assert left == {"u-event", "u-mail"}, (left, buf.getvalue())


def test_a_merge_of_two_same_day_applications_keeps_what_each_one_knew():
    con = store.connect(":memory:")
    a, b = _pair(con)
    store.record_application(con, a, applied_on="2026-10-06", route="LinkedIn Easy Apply",
                             reference="LI-1", contact="Wren <wren@wise.com>", source="cli")
    store.record_application(con, b, applied_on="2026-10-06", route="SmartRecruiters",
                             reference="", source="cli")
    store.add_event(con, a, "rejected", at="2026-10-07",
                    detail="LinkedIn copy rejection text", source="manual")
    store.add_event(con, b, "rejected", at="2026-10-07", detail="SR copy", source="manual")
    assert store.merge_duplicates(con) == 1
    (app,) = store.applications_for(con, b)
    # The survivor's own values stand; its blanks are filled from the loser.
    assert (app["route"], app["reference"], app["contact"]) == (
        "SmartRecruiters", "LI-1", "Wren <wren@wise.com>"), app
    (rej,) = [e for e in store.events_for(con, b) if e["kind"] == "rejected"]
    assert rej["detail"] == "LinkedIn copy rejection text"


def test_an_application_written_during_a_merge_is_never_cascaded_away():
    """Review finding 18. `merge_duplicates` moved the history, then deleted the
    losing row, in autocommit: an `applied` written to the loser in between
    (a dashboard click while the scan merges) was deleted with it. A second
    connection writing at exactly that moment must either be refused, and so
    told, or have its row survive the merge."""
    d = Path(tempfile.mkdtemp())
    db = d / "t.db"
    con = store.connect(db)
    a, b = _pair(con)                                  # a loses to b
    real = store.move_application_history
    outcome = {}

    def move_then_race(con_, *, keep, lose):
        real(con_, keep=keep, lose=lose)
        other = sqlite3.connect(db, timeout=0)
        try:
            other.execute("INSERT INTO applications (uid, applied_on, source, created_at) "
                          "VALUES (?, '2026-10-08', 'dashboard', '2026-10-08')", (lose,))
            other.commit()
            outcome["written"] = True
        except sqlite3.OperationalError as e:
            outcome["refused"] = str(e)
        finally:
            other.close()

    store.move_application_history = move_then_race
    try:
        store.merge_duplicates(con)
    finally:
        store.move_application_history = real
    if outcome.get("written"):
        assert con.execute("SELECT COUNT(*) FROM applications WHERE source='dashboard'"
                           ).fetchone()[0] == 1, "the write landed and the merge deleted it"
    else:
        assert "locked" in outcome.get("refused", ""), outcome


def test_rescreen_remove_rechecks_a_role_at_the_moment_it_deletes_it():
    """Same race as the merge: the roles to remove are chosen first and
    deleted later, so an application recorded in between was cascaded away."""
    import contextlib
    import io
    from jobradar import cli
    d = Path(tempfile.mkdtemp())
    cfg = d / "config.yaml"
    cfg.write_text("titles:\n  include: ['engineering manager']\n"
                   "locations:\n  countries: ['UK']\n"
                   "sources:\n  use_bundled: false\n", encoding="utf-8")
    db = d / "t.db"
    con = store.connect(db)
    con.execute(
        "INSERT INTO roles (uid,company,title,url,location,platform,"
        "description,first_seen,last_seen) VALUES (?,?,?,?,?,?,?,?,?)",
        ("u-late", "Zzz", "Plumber", "https://x.example/late", "Manchester, United Kingdom",
         "ashby", "pipes", "2026-10-01", "2026-10-01"))
    con.close()
    real = cli._read_closing_dates

    def late_application(con_, **kw):
        real(con_, **kw)
        other = store.connect(db)
        store.record_application(other, "u-late", applied_on="2026-10-08", source="dashboard")
        other.close()

    cli._read_closing_dates = late_application
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["-c", str(cfg), "rescreen", "--db", str(db), "--remove"])
    finally:
        cli._read_closing_dates = real
    assert code == 0, buf.getvalue()
    con = store.connect(db)
    assert store.applied_on(con, "u-late") == "2026-10-08", buf.getvalue()
    assert "Removed 0" in buf.getvalue(), buf.getvalue()


def test_a_merge_re_points_the_candidates_an_unmatched_proposal_names():
    """Re-check with the reviewer's p1: with both copies stored, the proposal
    matched neither and named both as candidates; after the merge it still
    named the deleted one."""
    from jobradar import mailsync
    con = store.connect(":memory:")
    a, b = _pair(con)
    _propose_rejection(con)
    (p,) = mailsync.list_proposals(con)
    assert p["uid"] == "" and a in " ".join(p["warnings"])
    store.merge_duplicates(con)
    (p,) = mailsync.list_proposals(con)
    assert a not in " ".join(p["warnings"]) and b in " ".join(p["warnings"]), p["warnings"]
