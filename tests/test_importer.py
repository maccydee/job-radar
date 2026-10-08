import contextlib
import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import cli, importer, store     # noqa: E402
from jobradar.models import Job               # noqa: E402


def _role(con, company, title, status, note, updated="2026-10-06"):
    j = Job(company=company, title=title, url=f"https://x.example/{company}/{title}",
            platform="custom", location="London")
    store.upsert_roles(con, [j], run=1)
    store.set_status(con, j.uid, status, note)
    con.execute("UPDATE role_state SET updated_at=? WHERE uid=?", (updated, j.uid))
    return j.uid


def test_a_dated_note_gives_the_date_and_route():
    con = store.connect(":memory:")
    uid = _role(con, "Data Lantern", "AI Engineering Tech Lead", "applied",
                "Applied 6 Oct 2026 via LinkedIn Easy Apply")
    plan = importer.plan_from_state(con)
    row = [p for p in plan if p.uid == uid][0]
    assert (row.applied_on, row.route, row.source) == ("2026-10-06", "LinkedIn Easy Apply", "import:note")


def test_an_undated_note_uses_updated_at_and_says_so():
    con = store.connect(":memory:")
    uid = _role(con, "Acme", "EM", "applied", "awaiting response", updated="2026-09-12")
    row = importer.plan_from_state(con)[0]
    assert row.applied_on == "2026-09-12" and row.estimated is True
    assert row.source == "import:updated_at"


def test_a_date_with_no_applied_cue_is_not_taken_for_the_application_date():
    """"deadline 6 Aug" and a bare "4 Aug" are in this database's own notes. A
    deadline is not a send date, and a bare date could be any of three things."""
    con = store.connect(":memory:")
    _role(con, "Acme", "EM", "applied", "deadline 6 Aug · strongest fit", updated="2026-08-20")
    _role(con, "Beta", "EM", "submitted", "4 Aug · 25 alumni inside", updated="2026-08-21")
    rows = {p.uid: p for p in importer.plan_from_state(con)}
    assert sorted((r.applied_on, r.estimated) for r in rows.values()) == [
        ("2026-08-20", True), ("2026-08-21", True)]


def test_a_year_less_date_takes_the_most_recent_one_not_after_the_status_change():
    con = store.connect(":memory:")
    _role(con, "Acme", "EM", "applied", "sent 28 Dec", updated="2026-01-10")
    assert importer.plan_from_state(con)[0].applied_on == "2025-12-28"


def test_a_rejected_role_does_not_invent_an_applied_date_from_the_rejection_date():
    con = store.connect(":memory:")
    uid = _role(con, "Thornfield Systems", "Security Engineering Manager", "rejected",
                "REJECTED 6 Oct 2026 (Pat Cutler)", updated="2026-10-07")
    row = importer.plan_from_state(con)[0]
    # The applied date is unknown: it cannot be later than the rejection, so
    # the estimate is capped there and says it is an estimate.
    assert row.estimated is True and row.source == "import:updated_at"
    assert row.applied_on == "2026-10-06"
    assert (row.final_status, row.final_on) == ("rejected", "2026-10-06")
    assert importer.apply_plan(con, [row]) == {"written": 1, "skipped": 0}
    kinds = [(e["kind"], e["at"]) for e in store.events_for(con, uid)]
    assert ("rejected", "2026-10-06") in kinds


def test_statuses_that_mean_nothing_was_sent_are_not_imported():
    con = store.connect(":memory:")
    for st in ("new", "interested", "skipped", "closed"):
        _role(con, f"Co{st}", "EM", st, "")
    assert importer.plan_from_state(con) == []


def test_dry_run_writes_nothing_and_apply_is_idempotent():
    con = store.connect(":memory:")
    _role(con, "Acme", "EM", "applied", "Applied 6 Oct 2026")
    plan = importer.plan_from_state(con)
    assert con.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0
    assert importer.apply_plan(con, plan) == {"written": 1, "skipped": 0}
    assert importer.apply_plan(con, importer.plan_from_state(con)) == {"written": 0, "skipped": 0}


def _handoff(text=None):
    md = Path(tempfile.mkdtemp()) / "applications-2026-09-26.md"
    md.write_text(
        text or "# Applications, 26 September 2026\n\n"
        "| Role | Company | Route | CV |\n|---|---|---|---|\n"
        "| Security Engineering Manager | Thornfield Systems, London | Ashby | Head of Security |\n"
        "| Head of Engineering, £150k | VirtueTech (energy trading) | direct email | Engineering Management |\n",
        encoding="utf-8")
    return md


def test_handoff_row_matches_a_role_and_unmatched_rows_are_listed_not_created():
    con = store.connect(":memory:")
    uid = _role(con, "Thornfield Systems", "Security Engineering Manager", "applied", "")
    rows, unmatched = importer.plan_from_handoff(con, _handoff())
    assert [(r.uid, r.applied_on, r.route) for r in rows] == [(uid, "2026-09-26", "Ashby")]
    assert rows[0].cv_label == "Head of Security" and rows[0].source == "import:handoff"
    assert [u["company"] for u in unmatched] == ["VirtueTech (energy trading)"]
    assert con.execute("SELECT COUNT(*) FROM roles").fetchone()[0] == 1


def test_an_unreadable_handoff_file_is_an_error_not_a_skip():
    con = store.connect(":memory:")
    try:
        importer.plan_from_handoff(con, Path("/nonexistent/applications.md"))
    except FileNotFoundError:
        return
    raise AssertionError("a missing file was skipped silently")


def test_a_handoff_with_no_date_anywhere_is_refused():
    con = store.connect(":memory:")
    md = Path(tempfile.mkdtemp()) / "applications.md"
    md.write_text("# Applications\n\n| Role | Company | Route | CV |\n|---|---|---|---|\n", encoding="utf-8")
    try:
        importer.plan_from_handoff(con, md)
    except ValueError as e:
        assert "no date" in str(e)
        return
    raise AssertionError("a file with no date was read as though it had one")


def test_two_roles_matching_one_handoff_row_are_ambiguous_and_listed():
    con = store.connect(":memory:")
    _role(con, "Thornfield Systems", "Security Engineering Manager", "applied", "")
    j = Job(company="Thornfield Systems", title="Security Engineering Manager",
            url="https://other.example/2", platform="custom", location="London")
    store.upsert_roles(con, [j], run=1)
    rows, unmatched = importer.plan_from_handoff(con, _handoff())
    assert rows == []
    assert [(u["company"], u["why"]) for u in unmatched][0] == ("Thornfield Systems, London", "2 roles match")


def test_the_handoff_date_replaces_an_estimate_and_fills_a_blank_route_only():
    con = store.connect(":memory:")
    uid = _role(con, "Thornfield Systems", "Security Engineering Manager", "applied",
                "awaiting response", updated="2026-10-01")
    plan = importer.build_plan(con, [_handoff()])
    (row,) = plan.rows
    assert (row.uid, row.applied_on, row.route, row.estimated, row.source) == (
        uid, "2026-09-26", "Ashby", False, "import:handoff")
    # A route the note already gave is kept, not overwritten by the table.
    con2 = store.connect(":memory:")
    _role(con2, "Thornfield Systems", "Security Engineering Manager", "applied",
          "Applied 26 Sep 2026 via Greenhouse")
    (row2,) = importer.build_plan(con2, [_handoff()]).rows
    assert (row2.route, row2.applied_on, row2.source) == ("Greenhouse", "2026-09-26", "import:note")


def test_a_handoff_row_for_a_role_already_recorded_is_counted_not_planned():
    con = store.connect(":memory:")
    uid = _role(con, "Thornfield Systems", "Security Engineering Manager", "applied", "")
    store.record_application(con, uid, applied_on="2026-09-26", route="Ashby")
    plan = importer.build_plan(con, [_handoff()])
    assert plan.rows == [] and plan.already_recorded == 1


def _cli_db():
    d = Path(tempfile.mkdtemp())
    cfg = d / "config.yaml"
    cfg.write_text("titles:\n  include: ['engineering manager']\n"
                   "locations:\n  countries: ['UK']\n"
                   "sources:\n  use_bundled: false\n", encoding="utf-8")
    db = d / "t.db"
    con = store.connect(db)
    _role(con, "Thornfield Systems", "Security Engineering Manager", "applied", "")
    _role(con, "Acme", "EM", "rejected", "REJECTED 6 Oct 2026", updated="2026-10-07")
    _role(con, "Skipco", "EM", "skipped", "")
    con.close()
    return cfg, db


def _run(cfg, *argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main(["-c", str(cfg), *argv])
    return code, buf.getvalue()


def test_the_command_is_a_dry_run_until_told_otherwise():
    cfg, db = _cli_db()
    md = _handoff()
    code, out = _run(cfg, "import-applications", "--db", str(db), "--handoff", str(md))
    assert code == 0, out
    assert "dry run: re-run with --apply to write 2 applications" in out, out
    assert "ESTIMATED" not in out.split("Thornfield Systems")[1].splitlines()[0]    # dated by the handoff
    assert "not in database:" in out and "VirtueTech" in out, out
    con = store.connect(db)
    assert con.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM app_events").fetchone()[0] == 0


def test_apply_writes_once_and_changes_no_status_or_note():
    cfg, db = _cli_db()
    con = store.connect(db)
    before = [tuple(r) for r in con.execute("SELECT uid,status,note,updated_at FROM role_state ORDER BY uid")]
    con.close()
    md = _handoff()
    code, out = _run(cfg, "import-applications", "--db", str(db), "--handoff", str(md), "--apply")
    assert code == 0 and "wrote 2" in out, out
    code, out = _run(cfg, "import-applications", "--db", str(db), "--handoff", str(md), "--apply")
    assert code == 0 and "wrote 0" in out, out
    con = store.connect(db)
    assert con.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 2
    after = [tuple(r) for r in con.execute("SELECT uid,status,note,updated_at FROM role_state ORDER BY uid")]
    assert before == after


def test_an_explicit_handoff_that_cannot_be_read_stops_the_command():
    cfg, db = _cli_db()
    code, out = _run(cfg, "import-applications", "--db", str(db), "--handoff", "/nonexistent/x.md")
    assert code == 1 and str(Path("/nonexistent/x.md")) in out, out
    con = store.connect(db)
    assert con.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0


def test_a_route_that_is_a_web_address_keeps_its_dots():
    """The first real run read "via jobs.bluetree.com" as the route "jobs": the stop
    at a full stop cut the address at its first dot."""
    con = store.connect(":memory:")
    _role(con, "Bluetree", "Software Engineering Manager", "applied",
          "Applied 6 Oct 2026 via jobs.bluetree.com. Closes 9 Oct.")
    _role(con, "Acme", "EM", "applied", "Applied 6 Oct 2026 via Greenhouse. Awaiting reply.")
    routes = sorted(p.route for p in importer.plan_from_state(con))
    assert routes == ["Greenhouse", "jobs.bluetree.com"], routes


# ------------------------------------- review finding 6: future interview dates

def test_an_upcoming_interview_is_not_moved_back_a_year_and_does_not_date_the_application():
    con = store.connect(":memory:")
    uid = _role(con, "Acme", "EM", "interviewing", "Interview 12 Oct", updated="2026-10-08")
    (row,) = importer.plan_from_state(con)
    assert (row.final_status, row.final_on) == ("interviewing", "2026-10-12"), row
    assert row.applied_on == "2026-10-08" and row.estimated is True, row
    importer.apply_plan(con, [row])
    assert ("2026-10-12", "interviewing") in [(e["at"], e["kind"]) for e in store.events_for(con, uid)]
    assert "2025" not in " ".join(store.duplicate_refusal(con, uid, "cv")[0])


def test_an_interview_after_a_stated_application_is_filed_after_it():
    con = store.connect(":memory:")
    _role(con, "Bluetree", "EM", "interviewing", "Applied 30 Sep via jobs.bluetree.com. Interview 12 Oct",
          updated="2026-10-08")
    (row,) = importer.plan_from_state(con)
    assert (row.applied_on, row.final_on) == ("2026-09-30", "2026-10-12"), row


def test_a_future_offer_date_is_kept_in_the_future():
    con = store.connect(":memory:")
    _role(con, "Acme", "EM", "offer", "offer 20 Oct, applied 1 Oct", updated="2026-10-08")
    (row,) = importer.plan_from_state(con)
    assert (row.applied_on, row.final_on) == ("2026-10-01", "2026-10-20"), row


def test_a_past_interview_is_still_read_as_the_nearest_occurrence():
    con = store.connect(":memory:")
    _role(con, "Acme", "EM", "interviewing", "interviewed 28 Dec", updated="2027-01-04")
    (row,) = importer.plan_from_state(con)
    assert row.final_on == "2026-12-28", row


def test_an_iso_date_in_a_note_is_read_like_any_other():
    """Re-check with the reviewer's p5, same shape as finding 19: "Applied
    2026-09-14 via Workday" was not read, and the application was dated from
    updated_at (labelled ESTIMATED, but the note had said)."""
    con = store.connect(":memory:")
    _role(con, "Acme", "EM", "applied", "Applied 2026-09-14 via Workday", updated="2026-09-20")
    _role(con, "Beta", "EM", "rejected", "applied 2026-09-01, rejected 2026-09-15",
          updated="2026-09-20")
    rows = {p.route or p.final_status: p for p in importer.plan_from_state(con)}
    assert (rows["Workday"].applied_on, rows["Workday"].estimated) == ("2026-09-14", False)
    assert (rows["rejected"].applied_on, rows["rejected"].final_on) == ("2026-09-01", "2026-09-15")
    _role(con, "Gamma", "EM", "applied", "Applied 2026-02-31", updated="2026-03-05")
    gamma = [p for p in importer.plan_from_state(con) if p.applied_on == "2026-03-05"]
    assert gamma and gamma[0].estimated is True         # an impossible date is not read
