import ast
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import cli, followups, store    # noqa: E402
from jobradar.models import Job               # noqa: E402

TODAY = date(2026, 10, 8)
CONTACT = "Wren Jarvis <wren@x.example>"


def _ago(n):
    return (TODAY - timedelta(days=n)).isoformat()


def _con(applied_days_ago=12, status="applied", contact=CONTACT, company="Acme",
         title="Engineering Manager", route="Ashby"):
    con = store.connect(":memory:")
    j = Job(company=company, title=title, url="https://x.example/1", platform="custom",
            location="London")
    store.upsert_roles(con, [j], run=1)
    store.record_application(con, j.uid, applied_on=_ago(applied_days_ago), route=route,
                             contact=contact)
    store.set_status(con, j.uid, status)
    return con, j.uid


def test_twelve_days_with_no_reply_is_quiet():
    con, uid = _con(12)
    (item,) = followups.quiet(con, TODAY, days=10)
    assert item["uid"] == uid and item["silent_days"] == 12 and item["drafted"] == 0


def test_six_days_is_not_quiet_yet():
    con, _ = _con(6)
    assert followups.quiet(con, TODAY, days=10) == []


def test_any_later_event_means_it_has_not_gone_quiet():
    for kind in ("acknowledged", "interviewing", "rejected"):
        con, uid = _con(12)
        store.add_event(con, uid, kind, at=_ago(5), source="mail:x")
        assert followups.quiet(con, TODAY, days=10) == [], kind


def test_an_acknowledgement_on_the_day_it_went_in_counts_as_a_reply():
    con, uid = _con(12)
    store.add_event(con, uid, "acknowledged", at=_ago(12), source="mail:x")
    assert followups.quiet(con, TODAY, days=10) == []


def test_a_cv_label_note_and_a_draft_are_not_replies():
    con, uid = _con(12)
    store.add_event(con, uid, "note", at=_ago(12), detail="CV sent: x", source="import:handoff")
    store.add_event(con, uid, "followup_drafted", at=_ago(2), source="followups")
    assert len(followups.quiet(con, TODAY, days=10)) == 1


def test_settled_and_later_statuses_are_not_listed():
    for status in ("rejected", "withdrawn", "interviewing", "offer"):
        con, _ = _con(12, status=status)
        assert followups.quiet(con, TODAY, days=10) == [], status


def test_a_board_still_open_is_listed_with_the_date_and_gets_no_draft():
    con, uid = _con(12)
    store.set_closing(con, uid, "2026-10-23", "by hand")
    (item,) = followups.quiet(con, TODAY, days=10)
    assert followups.reason_no_draft(item, TODAY) == "board still open until 2026-10-23"
    out = Path(tempfile.mkdtemp())
    assert followups.write_drafts(con, [item], out, TODAY, name="Alex") == []
    assert list(out.iterdir()) == []


def test_a_closed_board_does_not_block_the_draft():
    con, uid = _con(12)
    store.set_closing(con, uid, "2026-10-01", "by hand")
    (item,) = followups.quiet(con, TODAY, days=10)
    assert followups.reason_no_draft(item, TODAY) is None


def test_no_contact_is_listed_and_not_drafted():
    con, uid = _con(12, contact="")
    (item,) = followups.quiet(con, TODAY, days=10)
    assert followups.reason_no_draft(item, TODAY) == "no contact on file, nothing to draft"


def test_two_drafts_already_means_leave_it():
    con, uid = _con(30)
    store.add_event(con, uid, "followup_drafted", at=_ago(20), source="followups")
    store.add_event(con, uid, "followup_drafted", at=_ago(10), source="followups")
    (item,) = followups.quiet(con, TODAY, days=10)
    assert item["drafted"] == 2
    assert followups.reason_no_draft(item, TODAY) == "two follow-ups already drafted; leave it"
    assert followups.write_drafts(con, [item], Path(tempfile.mkdtemp()), TODAY, name="Alex") == []


def test_the_draft_says_only_what_the_record_says():
    con, uid = _con(12)
    (item,) = followups.quiet(con, TODAY, days=10)
    text = followups.draft(item, "Alex")
    assert text.startswith("Hi Wren,")
    for fact in ("Engineering Manager", "Acme", _ago(12), "Ashby"):
        assert fact in text, fact
    assert text.rstrip().endswith("Thanks,\nAlex")
    assert chr(0x2014) not in text
    # No claim about the person: no number, no skill, no past employer.
    assert not any(ch.isdigit() for ch in text.replace(_ago(12), ""))


def test_a_contact_that_is_a_bare_address_is_greeted_neutrally():
    con, uid = _con(12, contact="wren@x.example")
    (item,) = followups.quiet(con, TODAY, days=10)
    assert followups.draft(item, "Alex").startswith("Hi there,")


def test_a_missing_route_is_left_out_not_invented():
    # Review finding 17: this test used to assert "through your careers page",
    # a route the record does not hold, written into a message to an employer.
    con, uid = _con(12, route="")
    (item,) = followups.quiet(con, TODAY, days=10)
    text = followups.draft(item, "Alex")
    assert "through" not in text and "careers page" not in text, text
    assert f"role at Acme on {_ago(12)} and wanted to check" in text, text


def test_writing_a_draft_records_the_event_and_the_file_once():
    con, uid = _con(12)
    (item,) = followups.quiet(con, TODAY, days=10)
    out = Path(tempfile.mkdtemp())
    (path,) = followups.write_drafts(con, [item], out, TODAY, name="Alex")
    assert path.parent == out and path.name.startswith("followup-") and path.suffix == ".md"
    assert "Hi Wren," in path.read_text(encoding="utf-8")
    assert [e["kind"] for e in store.events_for(con, uid)].count("followup_drafted") == 1
    # The same day again: the file exists, it is not overwritten and not counted twice.
    assert followups.write_drafts(con, followups.quiet(con, TODAY, days=10), out, TODAY,
                                  name="Alex") == []
    assert [e["kind"] for e in store.events_for(con, uid)].count("followup_drafted") == 1


def test_the_draft_passes_the_house_prose_detector():
    detect = Path.home() / ".claude" / "skills" / "natural-writing" / "scripts" / "detect.py"
    if not detect.exists():
        raise unittest.SkipTest("natural-writing detect.py not installed")
    con, _ = _con(12)
    (item,) = followups.quiet(con, TODAY, days=10)
    f = Path(tempfile.mkdtemp()) / "draft.md"
    f.write_text(followups.draft(item, "Alex"), encoding="utf-8")
    r = subprocess.run([sys.executable, str(detect), str(f)], capture_output=True, text=True,
                       encoding="utf-8", stdin=subprocess.DEVNULL, timeout=60)
    # The report's own legend says "no FAILs", so the verdict line is what is read.
    import re
    m = re.search(r"SLOP SCORE: (\d+)/100.*->\s+(\w+)", r.stdout)
    assert m and m.group(2) == "PASS" and int(m.group(1)) <= 20, r.stdout


def test_the_module_cannot_send_anything():
    src = Path(followups.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
            imported |= {a.name for a in node.names} if not node.module else set()
    banned = {"smtplib", "imaplib", "urllib", "requests", "subprocess", "socket", "http",
              "ssl", "email", "runner", "fetch"}
    assert not imported & banned, imported & banned


# --------------------------------------------------------------------- CLI

def _cli(days_ago=12, **kw):
    d = Path(tempfile.mkdtemp())
    cfg = d / "config.yaml"
    cfg.write_text("titles:\n  include: ['engineering manager']\nlocations:\n  countries: ['UK']\n"
                   "sources:\n  use_bundled: false\n", encoding="utf-8")
    db = d / "t.db"
    con = store.connect(db)
    j = Job(company="Acme", title="Engineering Manager", url="https://x.example/1",
            platform="custom", location="London")
    store.upsert_roles(con, [j], run=1)
    store.record_application(con, j.uid, applied_on=(date.today() - timedelta(days=days_ago)).isoformat(),
                             route="Ashby", contact=kw.get("contact", CONTACT))
    store.set_status(con, j.uid, "applied")
    con.close()
    return d, cfg, db


def _run(cfg, *argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main(["-c", str(cfg), "followups", *argv])
    return code, buf.getvalue()


def test_cli_lists_the_quiet_applications_and_says_nothing_was_sent():
    d, cfg, db = _cli()
    code, out = _run(cfg, "--db", str(db))
    assert code == 0, out
    assert "Acme" in out and "12" in out and "Ashby" in out, out
    assert out.rstrip().endswith("drafts only: nothing was sent"), out
    assert list(d.glob("followup-*")) == []                    # no --write, no files


def test_cli_write_makes_the_draft_where_it_says():
    d, cfg, db = _cli()
    out_dir = d / "drafts"
    code, out = _run(cfg, "--db", str(db), "--write", str(out_dir), "--name", "Alex")
    assert code == 0, out
    (path,) = list(out_dir.glob("followup-*.md"))
    assert str(path) in out
    assert "Thanks,\nAlex" in path.read_text(encoding="utf-8")


def test_cli_without_a_name_leaves_a_visible_placeholder():
    d, cfg, db = _cli()
    out_dir = d / "drafts"
    # No claims file in view, whatever this machine has: a real claims.local.yaml
    # carries an `author`, and the test must say the same on every checkout.
    from jobradar import cvcheck
    real = cvcheck._search_paths
    cvcheck._search_paths = lambda: []
    try:
        code, out = _run(cfg, "--db", str(db), "--write", str(out_dir))
    finally:
        cvcheck._search_paths = real
    (path,) = list(out_dir.glob("followup-*.md"))
    assert "[your name]" in path.read_text(encoding="utf-8")
    assert "[your name]" in out and "--name" in out


def test_cli_with_nothing_quiet_says_so():
    d, cfg, db = _cli(days_ago=2)
    code, out = _run(cfg, "--db", str(db))
    assert code == 0 and "no applications have gone quiet" in out, out


def test_cli_days_must_be_a_whole_number_of_days():
    d, cfg, db = _cli()
    try:
        _run(cfg, "--db", str(db), "--days", "-3")
    except SystemExit as e:
        assert e.code == 2
        return
    raise AssertionError("a negative number of days was accepted")
