"""Review finding 10: a date estimated from `updated_at` is shown as one.

`updated_at` is when a status last changed, not when an application went in.
The importer files such a date under source `import:updated_at` and labels it
ESTIMATED in its own output, but every later reader printed it as a fact: the
duplicate guard said "was already applied for on X", the interview pack said
"Applied on: X", and a follow-up draft told an employer the day.
"""
import contextlib
import io
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import cli, followups, importer, interviewprep, store    # noqa: E402
from jobradar.models import Job                                        # noqa: E402

LABEL = "about 2026-09-12 (estimated from when the status last changed)"


def _role(con, company="Acme", title="Engineering Manager", status="applied", note="awaiting",
          updated="2026-09-12", url="https://x.example/1"):
    j = Job(company=company, title=title, url=url, platform="custom", location="London",
            description="We are hiring. " * 20)
    store.upsert_roles(con, [j], run=1)
    store.set_status(con, j.uid, status, note)
    con.execute("UPDATE role_state SET updated_at=? WHERE uid=?", (updated, j.uid))
    return j.uid


def _imported(con, **kw):
    uid = _role(con, **kw)
    plan = importer.plan_from_state(con)
    importer.apply_plan(con, [p for p in plan if p.uid == uid])
    return uid


def test_the_duplicate_guard_says_an_imported_date_is_an_estimate():
    con = store.connect(":memory:")
    uid = _imported(con)
    (sentences, same) = store.duplicate_refusal(con, uid, "cv")
    assert same and LABEL in sentences[0], sentences


def test_a_sibling_refusal_says_so_too():
    con = store.connect(":memory:")
    _imported(con)
    other = _role(con, url="https://x.example/2", status="new", note=None)
    (sentences, same) = store.duplicate_refusal(con, other, "cv")
    assert not same and LABEL in sentences[0], sentences


def test_a_known_date_is_printed_plainly():
    con = store.connect(":memory:")
    uid = _role(con, note="Applied 6 Sep 2026 via Ashby")
    importer.apply_plan(con, importer.plan_from_state(con))
    (sentences, _) = store.duplicate_refusal(con, uid, "cv")
    assert "on 2026-09-06 via Ashby" in sentences[0] and "estimated" not in sentences[0]


def test_the_interview_pack_says_the_applied_date_is_estimated():
    con = store.connect(":memory:")
    uid = _imported(con)
    pack = interviewprep.build_pack(con, uid, "technical", date(2026, 10, 8))
    assert f"- Applied on: {LABEL}" in pack, pack


def test_a_follow_up_draft_does_not_tell_an_employer_an_estimated_date():
    con = store.connect(":memory:")
    uid = _imported(con)
    con.execute("UPDATE applications SET contact='Wren <wren@x.example>', route='Ashby'")
    (item,) = followups.quiet(con, date(2026, 10, 8), days=10)
    text = followups.draft(item, "Alex")
    assert "2026-09-12" not in text and "Engineering Manager" in text, text


def test_the_final_event_takes_the_source_its_own_date_came_from():
    """The note dated the application, not the rejection, so the rejection is
    dated from updated_at and must be filed as such."""
    con = store.connect(":memory:")
    uid = _role(con, status="rejected", note="Applied 6 Sep 2026 via Ashby", updated="2026-10-07")
    (row,) = importer.plan_from_state(con)
    importer.apply_plan(con, [row])
    rej = [e for e in store.events_for(con, uid) if e["kind"] == "rejected"]
    assert [(e["at"], e["source"]) for e in rej] == [("2026-10-07", "import:updated_at")], rej
    app = [e for e in store.events_for(con, uid) if e["kind"] == "applied"]
    assert [(e["at"], e["source"]) for e in app] == [("2026-09-06", "import:note")]


def test_cli_history_and_applied_label_an_estimated_date():
    d = Path(tempfile.mkdtemp())
    cfg = d / "config.yaml"
    cfg.write_text("titles:\n  include: ['engineering manager']\nlocations:\n  countries: ['UK']\n"
                   "sources:\n  use_bundled: false\n", encoding="utf-8")
    db = d / "t.db"
    con = store.connect(db)
    uid = _imported(con)
    con.close()
    for argv in (["history", uid], ["applied", uid]):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["-c", str(cfg), *argv, "--db", str(db)])
        assert code == 0 and LABEL in buf.getvalue(), buf.getvalue()
