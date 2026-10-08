import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import cli, mailsync, store     # noqa: E402
from jobradar.models import Job               # noqa: E402

FULL = ["inbox", "deleteditems", "junkemail"]


def _con_with_roles():
    con = store.connect(":memory:")
    uids = {}
    for company, title, status in (("Thornfield Systems", "Security Engineering Manager", "applied"),
                                   ("Tessera", "Principal, AI Transformation", "applied"),
                                   ("Acme", "Engineering Manager", "new")):
        j = Job(company=company, title=title, url=f"https://x.example/{company}", platform="custom",
                location="London")
        store.upsert_roles(con, [j], run=1)
        uids[company] = j.uid
        if status != "new":
            store.record_application(con, j.uid, applied_on="2026-09-26", route="ashby")
            store.set_status(con, j.uid, status)
    return con, uids


def _msg(mid, subject, body="", sender="", folder="inbox", received="2026-10-06T08:13:01Z"):
    return {"id": mid, "folder": folder, "received": received, "subject": subject,
            "sender": sender, "body": body}


REJECT_TM = _msg("m-tm", "Regarding your application for Security Engineering Manager",
                 "We have decided to move forward with other candidates.",
                 sender="pcutler@thornfieldsystems.net")
INTERVIEW_TIP = _msg("m-tip", "Tessera Interview - PLEASE CONFIRM",
                     "Your confirmed interview schedule is: Principal, AI Transformation, Oct 12.",
                     sender="daniel@tessera.com", received="2026-10-07T09:00:00Z")


def _payload(*msgs, folders=FULL):
    return {"folders_read": list(folders), "since": "2026-09-01", "messages": list(msgs)}


def _counts(con):
    return tuple(con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                 for t in ("role_state", "app_events", "applications", "mail_proposals"))


def _rows(con):
    return [dict(r) for r in con.execute("SELECT * FROM mail_proposals ORDER BY id")]


def test_propose_writes_proposals_and_nothing_else():
    con, uids = _con_with_roles()
    before = _counts(con)
    s = mailsync.propose(con, _payload(REJECT_TM, INTERVIEW_TIP))
    after = _counts(con)
    assert after[:3] == before[:3], (before, after)          # role_state, events, applications
    assert after[3] == before[3] + 2
    assert s["proposals"] == 2 and s["read"] == 2 and s["partial"] is False
    rows = {r["message_id"]: r for r in _rows(con)}
    assert rows["m-tm"]["new_status"] == "rejected" and rows["m-tm"]["uid"] == uids["Thornfield Systems"]
    assert rows["m-tip"]["new_status"] == "interviewing" and rows["m-tip"]["happens_at"] == "2026-10-07"
    assert all(r["state"] == "pending" for r in rows.values())


def test_a_second_run_of_the_same_file_adds_nothing():
    con, _ = _con_with_roles()
    mailsync.propose(con, _payload(REJECT_TM, INTERVIEW_TIP))
    s = mailsync.propose(con, _payload(REJECT_TM, INTERVIEW_TIP))
    assert s["proposals"] == 0 and s["already_proposed"] == 2
    assert len(_rows(con)) == 2


def test_a_rejection_for_a_role_not_recorded_as_applied_carries_a_warning():
    con, uids = _con_with_roles()
    msg = _msg("m-acme", "Update", "We have decided to move forward with other candidates.",
               sender="hr@acme.com")
    mailsync.propose(con, _payload(msg))
    (row,) = _rows(con)
    assert "role is not recorded as applied" in json.loads(row["warnings"])


def test_a_message_found_in_deleted_items_says_so():
    con, _ = _con_with_roles()
    mailsync.propose(con, _payload(dict(REJECT_TM, folder="deleteditems")))
    (row,) = _rows(con)
    assert "found in Deleted Items" in json.loads(row["warnings"])


def test_a_rejection_that_predates_the_application_is_flagged():
    con, _ = _con_with_roles()
    mailsync.propose(con, _payload(dict(REJECT_TM, received="2026-09-01T08:00:00Z")))
    (row,) = _rows(con)
    assert "message predates the application" in json.loads(row["warnings"])


def test_a_rejection_for_a_role_already_rejected_is_not_proposed():
    con, uids = _con_with_roles()
    store.transition(con, uids["Thornfield Systems"], "rejected", source="manual")
    s = mailsync.propose(con, _payload(REJECT_TM))
    assert s["proposals"] == 0 and s["already_current"] == 1 and _rows(con) == []


def test_ambiguous_and_other_mail_makes_no_proposal_but_is_counted():
    con, _ = _con_with_roles()
    amb = _msg("m-amb", "Your application", "Unfortunately we cannot progress. We would love to interview you for another role.",
               sender="x@thornfieldsystems.net")
    oth = _msg("m-oth", "Jane viewed your profile", "hello")
    s = mailsync.propose(con, _payload(amb, oth))
    assert s["other"] == 2 and s["proposals"] == 0 and _rows(con) == []
    assert [m["id"] for m in s["other_messages"]] == ["m-amb", "m-oth"]


def test_a_rejection_that_matches_no_role_is_kept_and_counted_as_unmatched():
    con, _ = _con_with_roles()
    msg = _msg("m-x", "Your application to Nowhere Ltd", "We have decided to move forward with other candidates.",
               sender="hr@nowhere.example")
    s = mailsync.propose(con, _payload(msg))
    assert s["unmatched"] == 1 and s["proposals"] == 0
    (row,) = _rows(con)
    assert row["uid"] == "" and "no role matched" in json.loads(row["warnings"])[0]


def test_an_offer_proposes_offer_with_a_warning():
    con, _ = _con_with_roles()
    msg = _msg("m-off", "Offer", "We are pleased to offer you the Principal, AI Transformation role.",
               sender="daniel@tessera.com")
    mailsync.propose(con, _payload(msg))
    (row,) = _rows(con)
    assert row["new_status"] == "offer"
    assert "an offer is your decision: check before applying" in json.loads(row["warnings"])


def test_an_acknowledgement_proposes_an_event_and_no_status():
    con, uids = _con_with_roles()
    msg = _msg("m-ack", "Thanks", "We have received your application for Principal, AI Transformation.",
               sender="daniel@tessera.com")
    mailsync.propose(con, _payload(msg))
    (row,) = _rows(con)
    assert row["new_status"] == "" and row["event_kind"] == "acknowledged"
    out = mailsync.apply_proposals(con, [row["id"]])
    assert out["applied"] == [row["id"]]
    assert store.status_of(con, uids["Tessera"]) == "applied"          # untouched
    assert [e["kind"] for e in store.events_for(con, uids["Tessera"])][-1] == "acknowledged"


def test_an_interview_never_moves_a_role_backwards_from_an_offer():
    con, uids = _con_with_roles()
    store.set_status(con, uids["Tessera"], "offer")
    mailsync.propose(con, _payload(INTERVIEW_TIP))
    (row,) = _rows(con)
    assert any("offer" in w for w in json.loads(row["warnings"]))


def test_unreadable_input_is_an_error_not_an_empty_run():
    con, _ = _con_with_roles()
    for bad in ([], "x", {"messages": "no"}, {"folders_read": FULL, "messages": [{"subject": "no id"}]}):
        try:
            mailsync.propose(con, bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")


def test_partial_reads_are_flagged_in_the_summary():
    con, _ = _con_with_roles()
    s = mailsync.propose(con, _payload(REJECT_TM, folders=["inbox"]))
    assert s["partial"] is True and s["missing"] == ["deleteditems", "junkemail"]
    s = mailsync.propose(con, _payload(folders=FULL))
    assert s["partial"] is True and s["read"] == 0
    s = mailsync.propose(con, _payload(INTERVIEW_TIP, folders=["Inbox", "Deleted Items", "Junk Email"]))
    assert s["partial"] is False             # the names Outlook shows are normalised


# ------------------------------------------------------------------- apply

def test_apply_moves_only_the_named_proposals_and_records_their_source():
    con, uids = _con_with_roles()
    mailsync.propose(con, _payload(REJECT_TM, INTERVIEW_TIP))
    ids = {r["message_id"]: r["id"] for r in _rows(con)}
    out = mailsync.apply_proposals(con, [ids["m-tm"]])
    assert out["applied"] == [ids["m-tm"]]
    assert store.status_of(con, uids["Thornfield Systems"]) == "rejected"
    assert store.status_of(con, uids["Tessera"]) == "applied"
    ev = [e for e in store.events_for(con, uids["Thornfield Systems"]) if e["kind"] == "rejected"]
    assert [(e["at"], e["source"]) for e in ev] == [("2026-10-06", "mail:m-tm")]
    assert "decided to move forward" in ev[0]["detail"]
    states = {r["message_id"]: r["state"] for r in _rows(con)}
    assert states == {"m-tm": "applied", "m-tip": "pending"}


def test_apply_names_and_skips_an_id_that_is_not_pending():
    con, uids = _con_with_roles()
    mailsync.propose(con, _payload(REJECT_TM))
    (row,) = _rows(con)
    mailsync.apply_proposals(con, [row["id"]])
    out = mailsync.apply_proposals(con, [row["id"], 999])
    assert out["applied"] == [] and {i for i, _ in out["skipped"]} == {row["id"], 999}


def test_apply_all_applies_only_the_clean_ones_and_lists_the_rest():
    con, uids = _con_with_roles()
    deleted = dict(REJECT_TM, id="m-del", folder="deleteditems")
    mailsync.propose(con, _payload(deleted, INTERVIEW_TIP))
    out = mailsync.apply_proposals(con, [], all_clean=True)
    assert len(out["applied"]) == 1 and len(out["needs_explicit"]) == 1
    assert store.status_of(con, uids["Tessera"]) == "interviewing"
    assert store.status_of(con, uids["Thornfield Systems"]) == "applied"


def test_an_unmatched_proposal_needs_a_role_and_then_applies():
    con, uids = _con_with_roles()
    msg = _msg("m-x", "Your application", "We have decided to move forward with other candidates.",
               sender="hr@nowhere.example")
    mailsync.propose(con, _payload(msg))
    (row,) = _rows(con)
    out = mailsync.apply_proposals(con, [row["id"]])
    assert out["applied"] == [] and "no role matched" in out["skipped"][0][1]
    out = mailsync.apply_proposals(con, [row["id"]], role_uid=uids["Thornfield Systems"])
    assert out["applied"] == [row["id"]]
    assert store.status_of(con, uids["Thornfield Systems"]) == "rejected"


def test_dismiss_is_permanent():
    con, _ = _con_with_roles()
    mailsync.propose(con, _payload(REJECT_TM))
    (row,) = _rows(con)
    assert mailsync.dismiss(con, [row["id"]]) == {"dismissed": [row["id"]], "skipped": []}
    s = mailsync.propose(con, _payload(REJECT_TM))
    assert s["proposals"] == 0 and s["already_proposed"] == 1
    assert _rows(con)[0]["state"] == "dismissed"


# --------------------------------------------------------------------- CLI

def _cli_db():
    d = Path(tempfile.mkdtemp())
    cfg = d / "config.yaml"
    cfg.write_text("titles:\n  include: ['engineering manager']\nlocations:\n  countries: ['UK']\n"
                   "sources:\n  use_bundled: false\n", encoding="utf-8")
    db = d / "t.db"
    con, uids = _con_with_roles()
    con2 = store.connect(db)
    for row in con.execute("SELECT * FROM roles"):
        j = Job(company=row["company"], title=row["title"], url=row["url"], platform="custom",
                location="London")
        store.upsert_roles(con2, [j], run=1)
    for uid in uids.values():
        r = con.execute("SELECT status FROM role_state WHERE uid=?", (uid,)).fetchone()
        if r and r["status"] != "new":
            store.record_application(con2, uid, applied_on="2026-09-26")
            store.set_status(con2, uid, r["status"])
    con2.close()
    return d, cfg, db


def _run(cfg, *argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main(["-c", str(cfg), "mail-sync", *argv])
    return code, buf.getvalue()


def _file(d, payload, name="mail.json"):
    p = d / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_cli_propose_prints_the_summary_and_writes_proposals_only():
    d, cfg, db = _cli_db()
    f = _file(d, _payload(REJECT_TM, INTERVIEW_TIP))
    code, out = _run(cfg, "propose", "--from", str(f), "--db", str(db))
    assert code == 0, out
    assert "read 2 messages (folders: inbox, deleteditems, junkemail), 2 proposals, 0 unmatched, 0 other" in out, out
    code, out = _run(cfg, "propose", "--from", str(f), "--db", str(db))
    assert "0 new, 2 already proposed" in out, out


def test_cli_a_partial_read_exits_3_unless_allowed():
    d, cfg, db = _cli_db()
    f = _file(d, _payload(REJECT_TM, folders=["inbox", "junkemail"]))
    code, out = _run(cfg, "propose", "--from", str(f), "--db", str(db))
    assert code == 3 and "WARNING: Deleted Items was not read; rejections are often filed there" in out, out
    f2 = _file(d, _payload(INTERVIEW_TIP, folders=["inbox", "junkemail"]), "m2.json")
    code, out = _run(cfg, "propose", "--from", str(f2), "--db", str(db), "--allow-partial")
    assert code == 0 and "WARNING" in out, out          # still said, just not fatal


def test_cli_an_empty_read_exits_3():
    d, cfg, db = _cli_db()
    code, out = _run(cfg, "propose", "--from", str(_file(d, _payload())), "--db", str(db))
    assert code == 3 and "read 0 messages" in out, out


def test_cli_an_unreadable_file_is_exit_1():
    d, cfg, db = _cli_db()
    code, out = _run(cfg, "propose", "--from", str(d / "nope.json"), "--db", str(db))
    assert code == 1 and "nope.json" in out
    bad = d / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    code, out = _run(cfg, "propose", "--from", str(bad), "--db", str(db))
    assert code == 1 and "bad.json" in out


def test_cli_list_apply_and_dismiss():
    d, cfg, db = _cli_db()
    _run(cfg, "propose", "--from", str(_file(d, _payload(REJECT_TM, INTERVIEW_TIP))), "--db", str(db))
    code, out = _run(cfg, "list", "--db", str(db))
    assert code == 0 and "applied -> rejected" in out and "applied -> interviewing" in out, out
    assert "2026-10-06" in out and "decided to move forward" in out, out
    code, out = _run(cfg, "apply", "--db", str(db))
    assert code == 1 and "name the proposals to apply, or pass --all" in out, out
    code, out = _run(cfg, "apply", "--all", "--db", str(db))
    assert code == 0 and "applied 2" in out, out
    code, out = _run(cfg, "list", "--db", str(db))
    assert "no pending proposals" in out, out
    con = store.connect(db)
    assert sorted(r["status"] for r in con.execute("SELECT status FROM role_state WHERE status<>'new'")) == [
        "interviewing", "rejected"]


def test_cli_apply_of_a_non_pending_id_exits_1_and_names_it():
    d, cfg, db = _cli_db()
    code, out = _run(cfg, "apply", "41", "--db", str(db))
    assert code == 1 and "41" in out and "not pending" in out, out
    code, out = _run(cfg, "dismiss", "41", "--db", str(db))
    assert code == 1 and "41" in out, out


def test_a_second_message_about_the_same_rejection_has_nothing_left_to_apply():
    con, uids = _con_with_roles()
    mailsync.propose(con, _payload(REJECT_TM, dict(REJECT_TM, id="m-copy", folder="deleteditems")))
    first, copy = [r["id"] for r in _rows(con)]
    assert mailsync.apply_proposals(con, [first])["applied"] == [first]
    out = mailsync.apply_proposals(con, [copy])
    assert out["applied"] == [] and "already rejected" in out["skipped"][0][1], out
    assert len([e for e in store.events_for(con, uids["Thornfield Systems"]) if e["kind"] == "rejected"]) == 1
    out = mailsync.apply_proposals(con, [], all_clean=True)
    assert out["applied"] == [] and out["needs_explicit"] == [copy]


# ------------------------------------------- review findings 2 and 3: apply

def _role(con, company, title, url, status=None, applied="2026-09-20"):
    j = Job(company=company, title=title, url=url, platform="custom", location="London")
    store.upsert_roles(con, [j], run=1)
    if status:
        store.record_application(con, j.uid, applied_on=applied, source="cli")
        store.transition(con, j.uid, status, source="cli", at=applied)
    return j.uid


def test_apply_all_in_newest_first_payload_order_never_ends_on_the_older_message():
    """Graph lists newest first. The rejection of 10-05 came before the invite of
    10-01 in the file, so it got the lower id, and applying in id order set
    rejected then interviewing: the role ended up interviewing."""
    con = store.connect(":memory:")
    t = _role(con, "Tessera", "Principal, AI Transformation", "https://x/t", "applied")
    mailsync.propose(con, _payload(
        _msg("new", "Tessera - Principal, AI Transformation",
             "Unfortunately, we have decided not to proceed forward with your application.",
             sender="a@tessera.com", received="2026-10-05T09:00:00Z"),
        _msg("old", "Tessera - Principal, AI Transformation",
             "We would love to interview you. Please book a time.",
             sender="a@tessera.com", received="2026-10-01T09:00:00Z")))
    ids = {r["message_id"]: r["id"] for r in _rows(con)}
    out = mailsync.apply_proposals(con, [], all_clean=True)
    # The invite is applied first, as it happened first. The rejection then
    # lands on an interviewing role, which carries the same-process warning,
    # so --all holds it back for an explicit id rather than deciding.
    assert out["applied"] == [ids["old"]] and out["needs_explicit"] == [ids["new"]], out
    assert store.status_of(con, t) == "interviewing"
    out = mailsync.apply_proposals(con, [ids["new"]])
    assert out["applied"] == [ids["new"]]
    assert store.status_of(con, t) == "rejected"
    assert [(e["at"], e["kind"]) for e in store.events_for(con, t)][-2:] == [
        ("2026-10-01", "interviewing"), ("2026-10-05", "rejected")]


def test_apply_all_never_reopens_a_rejected_role():
    con = store.connect(":memory:")
    t = _role(con, "Ocado", "Engineering Manager", "https://x/o")
    store.record_application(con, t, applied_on="2026-09-01", source="cli")
    store.transition(con, t, "rejected", source="cli", at="2026-09-20")
    mailsync.propose(con, _payload(_msg(
        "o1", "Ocado Technology - next steps",
        "We would love to interview you for our Platform team. Please book a time.",
        sender="r@ocado.com", received="2026-10-06T09:00:00Z")))
    (row,) = _rows(con)
    assert any("backwards" in w for w in json.loads(row["warnings"])), row["warnings"]
    out = mailsync.apply_proposals(con, [], all_clean=True)
    assert out["applied"] == [] and out["needs_explicit"] == [row["id"]]
    assert store.status_of(con, t) == "rejected"


def test_apply_all_recomputes_warnings_against_the_status_at_apply_time():
    """Proposed clean while the role was applied; rejected by hand since. The
    stored warnings are stale, so --all must look again."""
    con = store.connect(":memory:")
    t = _role(con, "Ocado", "Engineering Manager", "https://x/o", "applied")
    mailsync.propose(con, _payload(_msg(
        "o1", "Ocado - Engineering Manager", "We would love to interview you. Please book a time.",
        sender="r@ocado.com", received="2026-10-06T09:00:00Z")))
    (row,) = _rows(con)
    assert json.loads(row["warnings"]) == []
    store.transition(con, t, "rejected", source="cli", at="2026-10-07")
    out = mailsync.apply_proposals(con, [], all_clean=True)
    assert out["applied"] == [] and out["needs_explicit"] == [row["id"]]
    assert store.status_of(con, t) == "rejected"


def test_an_explicit_id_may_move_backwards_and_says_so():
    con = store.connect(":memory:")
    t = _role(con, "Ocado", "Engineering Manager", "https://x/o")
    store.record_application(con, t, applied_on="2026-09-01", source="cli")
    store.transition(con, t, "rejected", source="cli", at="2026-09-20")
    mailsync.propose(con, _payload(_msg(
        "o1", "Ocado - Engineering Manager", "We would love to interview you. Please book a time.",
        sender="r@ocado.com", received="2026-10-06T09:00:00Z")))
    (row,) = _rows(con)
    out = mailsync.apply_proposals(con, [row["id"]])
    assert out["applied"] == [row["id"]]
    assert any(i == row["id"] and "backwards" in w for i, w in out["warned"]), out
    assert store.status_of(con, t) == "interviewing"


def test_an_explicit_role_overrides_the_role_the_proposal_matched():
    con = store.connect(":memory:")
    monarch = _role(con, "Monarch", "Engineering Manager", "https://x/1", "applied")
    acme = _role(con, "Acme", "Head of Security", "https://x/2", "applied")
    mailsync.propose(con, _payload(_msg(
        "m2", "Update on your application",
        "Unfortunately we will not be progressing your application. (Monarch referred you.)",
        sender="talent@monarch.com", received="2026-10-07T09:00:00Z")))
    (row,) = _rows(con)
    assert row["uid"] == monarch
    out = mailsync.apply_proposals(con, [row["id"]], role_uid=acme)
    assert out["applied"] == [row["id"]]
    assert out["retargeted"] == [(row["id"], monarch, acme)]
    assert store.status_of(con, acme) == "rejected"
    assert store.status_of(con, monarch) == "applied"


def test_a_role_cannot_be_named_for_all():
    con = store.connect(":memory:")
    acme = _role(con, "Acme", "Head of Security", "https://x/2", "applied")
    try:
        mailsync.apply_proposals(con, [], all_clean=True, role_uid=acme)
    except ValueError:
        return
    raise AssertionError("--all with --role was accepted")


def test_cli_apply_with_role_names_the_role_it_applied_to():
    d, cfg, db = _cli_db()
    con = store.connect(db)
    acme = con.execute("SELECT uid FROM roles WHERE company='Acme'").fetchone()[0]
    con.close()
    _run(cfg, "propose", "--from", str(_file(d, _payload(REJECT_TM))), "--db", str(db))
    code, out = _run(cfg, "apply", "1", "--role", acme, "--db", str(db))
    assert code == 0 and "applied 1" in out and "Acme" in out and "instead of" in out, out
    con = store.connect(db)
    assert store.status_of(con, acme) == "rejected"
    code, out = _run(cfg, "apply", "--all", "--role", acme, "--db", str(db))
    assert code == 1 and "--role" in out, out


# --------------------------------------- review finding 11: Brightwell, again

ALREADY = _msg("k", "Brightwell - AI Lead", "It looks like you already applied for this position.",
               sender="noreply@brightwell.com", received="2026-10-01T09:00:00Z")


def _brightwell_new():
    con = store.connect(":memory:")
    return con, _role(con, "Brightwell", "AI Lead", "https://x/3")


def test_you_already_applied_on_an_unrecorded_role_proposes_recording_the_application():
    con, c = _brightwell_new()
    mailsync.propose(con, _payload(ALREADY))
    (row,) = _rows(con)
    assert "role is not recorded as applied" in json.loads(row["warnings"]), row
    assert row["new_status"] == "applied" and row["event_kind"] == "acknowledged"
    out = mailsync.apply_proposals(con, [], all_clean=True)
    assert out["applied"] == [] and out["needs_explicit"] == [row["id"]]   # warned: needs the id
    out = mailsync.apply_proposals(con, [row["id"]])
    assert out["applied"] == [row["id"]]
    (app,) = store.applications_for(con, c)
    assert app["applied_on"] == "2026-10-01" and app["source"] == "mail-ack:k", app
    assert store.status_of(con, c) == "applied"
    assert "acknowledged" in [e["kind"] for e in store.events_for(con, c)]
    sentences, same = store.duplicate_refusal(con, c, "cv")
    assert same and "on or before 2026-10-01" in sentences[0], sentences


def test_an_acknowledgement_alone_already_blocks_a_second_cv():
    """Before the proposal is applied, the event on file is enough for the
    guard to say something rather than nothing."""
    con, c = _brightwell_new()
    store.add_event(con, c, "acknowledged", at="2026-10-01", source="mail:k",
                    detail="It looks like you already applied for this position.")
    assert store.duplicate_refusal(con, c, "cv") is not None


def test_an_acknowledgement_on_a_rejected_role_records_the_application_without_reopening_it():
    con, c = _brightwell_new()
    store.transition(con, c, "rejected", source="cli", at="2026-10-05")
    mailsync.propose(con, _payload(ALREADY))
    (row,) = _rows(con)
    assert row["new_status"] == "", row
    mailsync.apply_proposals(con, [row["id"]])
    assert store.status_of(con, c) == "rejected"
    assert len(store.applications_for(con, c)) == 1


def test_an_acknowledgement_for_a_recorded_application_records_nothing_more():
    con, uids = _con_with_roles()
    msg = _msg("m-ack2", "Thanks", "We have received your application for Principal, AI Transformation.",
               sender="daniel@tessera.com")
    mailsync.propose(con, _payload(msg))
    (row,) = _rows(con)
    assert json.loads(row["warnings"]) == [] and row["new_status"] == ""
    mailsync.apply_proposals(con, [row["id"]])
    assert len(store.applications_for(con, uids["Tessera"])) == 1


def test_an_acknowledgement_retargeted_to_a_rejected_role_does_not_reopen_it():
    con, c = _brightwell_new()
    other = _role(con, "Brightwell", "Head of AI", "https://x/4")
    store.record_application(con, other, applied_on="2026-09-01", source="cli")
    store.transition(con, other, "rejected", source="cli", at="2026-09-20")
    mailsync.propose(con, _payload(ALREADY))
    (row,) = _rows(con)
    assert row["uid"] == c and row["new_status"] == "applied"
    mailsync.apply_proposals(con, [row["id"]], role_uid=other)
    assert store.status_of(con, other) == "rejected"


# ------------------------------- review finding 20: a second-round invite

def test_a_second_round_invite_on_an_interviewing_role_adds_an_event_and_keeps_the_status():
    con = store.connect(":memory:")
    t = _role(con, "Tessera", "Principal, AI Transformation", "https://x/t", "applied")
    store.transition(con, t, "interviewing", source="cli", at="2026-10-01")
    second = _msg("r2", "Tessera - Principal, AI Transformation",
                  "We would love to invite you to the second round. Please book a time.",
                  sender="a@tessera.com", received="2026-10-09T09:00:00Z")
    s = mailsync.propose(con, _payload(second))
    assert s["proposals"] == 1 and s["already_current"] == 0, s
    (row,) = _rows(con)
    assert row["new_status"] == "" and row["event_kind"] == "interviewing"
    assert json.loads(row["warnings"]) == []
    out = mailsync.apply_proposals(con, [], all_clean=True)
    assert out["applied"] == [row["id"]], out
    assert store.status_of(con, t) == "interviewing"
    ev = [e for e in store.events_for(con, t) if e["kind"] == "interviewing"]
    assert [e["at"] for e in ev] == ["2026-10-01", "2026-10-09"]
    assert "second round" in ev[-1]["detail"], ev[-1]


def test_an_invite_no_later_than_the_last_interview_event_is_already_current():
    con = store.connect(":memory:")
    t = _role(con, "Tessera", "Principal, AI Transformation", "https://x/t", "applied")
    store.transition(con, t, "interviewing", source="cli", at="2026-10-09")
    s = mailsync.propose(con, _payload(_msg(
        "r1", "Tessera - Principal, AI Transformation",
        "We would love to interview you. Please book a time.",
        sender="a@tessera.com", received="2026-10-05T09:00:00Z")))
    assert s["proposals"] == 0 and s["already_current"] == 1, s


def test_a_pending_you_already_applied_proposal_already_stops_a_cv():
    """Re-check of finding 11 with the reviewer's p8: until the proposal is
    applied, the guard still said nothing. An employer saying so is evidence."""
    con, c = _brightwell_new()
    mailsync.propose(con, _payload(ALREADY))
    sentences, same = store.duplicate_refusal(con, c, "cv")
    assert same and "2026-10-01" in sentences[0] and "mail-sync" in sentences[0], sentences
    other = _role(con, "Brightwell", "AI Lead", "https://x/linkedin-copy")
    sentences, same = store.duplicate_refusal(con, other, "cv")
    assert not same and c in sentences[0], sentences
