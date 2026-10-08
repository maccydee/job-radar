import ast
import contextlib
import io
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import cli, interviewprep, runner, store    # noqa: E402
from jobradar.models import Job                            # noqa: E402

TODAY = date(2026, 10, 8)
JD = ("About the role\n\nYou will lead the platform group.\n"
      "- Must have Kubernetes and Terraform in production.\n"
      "- Experience hiring and developing engineers.\n"
      "- There is a coding round in the process.\n"
      "Closing date: 23rd October 2026\n")
CV = "I run Kubernetes clusters in production and I do hiring and developing of engineers.\n"


def _setup(jd=JD, cv=CV, company="Acme", title="Head of Platform", application=True):
    con = store.connect(":memory:")
    j = Job(company=company, title=title, url="https://x.example/1", platform="custom",
            location="London", description="")
    store.upsert_roles(con, [j], run=1)
    if jd is not None:
        store.add_artifact(con, j.uid, "jd_snapshot", path="/docs/acme/jd.md", body=jd)
    if cv is not None:
        store.add_artifact(con, j.uid, "cv", path="/docs/acme/CV.docx", body=cv)
    if application:
        store.record_application(con, j.uid, applied_on="2026-09-27", route="jobylon",
                                 reference="18640464", salary_answer="145000",
                                 cv_path="/sent/CV-final.pdf")
    store.set_status(con, j.uid, "interviewing")
    return con, j.uid


def _section(pack, name):
    parts = pack.split("\n## ")
    for p in parts[1:]:
        if p.startswith(name):
            return p
    raise AssertionError(f"no section {name!r} in:\n{pack}")


def test_the_pack_has_every_section():
    con, uid = _setup()
    pack = interviewprep.build_pack(con, uid, "hiring manager", TODAY)
    for name in ("What they will have read", "What you told them", "The role", "Timeline",
                 "Where the CV is thin against the posting", "Questions to prepare for this stage",
                 "Evidence on file"):
        _section(pack, name)
    assert chr(0x2014) not in pack


def test_the_cv_they_read_is_the_one_recorded_on_the_application():
    con, uid = _setup()
    s = _section(interviewprep.build_pack(con, uid, "technical", TODAY), "What they will have read")
    assert "/sent/CV-final.pdf" in s and "recorded on the application" in s


def test_without_a_recorded_cv_the_newest_generated_one_is_named_and_said_to_be_a_guess():
    con, uid = _setup(application=False)
    s = _section(interviewprep.build_pack(con, uid, "technical", TODAY), "What they will have read")
    assert "/docs/acme/CV.docx" in s and "newest CV drafted by generate" in s
    assert "not recorded as sent" in s


def test_with_no_cv_at_all_it_says_to_find_the_file():
    con, uid = _setup(cv=None, application=False)
    s = _section(interviewprep.build_pack(con, uid, "technical", TODAY), "What they will have read")
    assert "no CV is recorded for this application: find the file you actually sent before the interview" in s


def test_what_you_told_them_has_the_salary_and_the_reference():
    con, uid = _setup()
    s = _section(interviewprep.build_pack(con, uid, "recruiter screen", TODAY), "What you told them")
    assert "145000" in s and "18640464" in s and "jobylon" in s


def test_blank_answers_are_said_to_be_not_recorded():
    con, uid = _setup(application=False)
    s = _section(interviewprep.build_pack(con, uid, "recruiter screen", TODAY), "What you told them")
    assert s.count("not recorded") >= 2


def test_the_cv_is_thin_where_the_posting_asks_for_what_it_lacks():
    con, uid = _setup()
    s = _section(interviewprep.build_pack(con, uid, "technical", TODAY),
                 "Where the CV is thin against the posting")
    assert "keyword check only; read the posting yourself" in s
    assert "not in the CV: Terraform" in s
    assert "Kubernetes" not in s.split("not in the CV:")[1].split("\n")[0]


def test_a_cv_that_covers_the_requirements_says_so_instead_of_staying_silent():
    con, uid = _setup(cv=CV + "I also write Terraform every day.")
    s = _section(interviewprep.build_pack(con, uid, "technical", TODAY),
                 "Where the CV is thin against the posting")
    assert "not in the CV" not in s and "no requirement line" in s


def test_no_posting_text_is_stated_and_the_thin_section_is_unmeasured_not_empty():
    con, uid = _setup(jd=None)
    pack = interviewprep.build_pack(con, uid, "technical", TODAY)
    assert "no posting text stored for this role" in _section(pack, "The role")
    thin = _section(pack, "Where the CV is thin against the posting")
    assert "UNMEASURED" in thin and "no requirement" not in thin


def test_no_cv_text_makes_the_thin_section_unmeasured_too():
    con, uid = _setup(cv=None)
    assert "UNMEASURED" in _section(interviewprep.build_pack(con, uid, "technical", TODAY),
                                    "Where the CV is thin against the posting")


def test_the_scans_own_description_is_used_when_no_snapshot_was_taken():
    con, uid = _setup(jd=None)
    con.execute("UPDATE roles SET description=? WHERE uid=?", (JD, uid))
    pack = interviewprep.build_pack(con, uid, "technical", TODAY)
    assert "no posting text stored" not in pack and "not in the CV: Terraform" in pack


def test_each_stage_asks_its_own_questions():
    con, uid = _setup()
    rec = _section(interviewprep.build_pack(con, uid, "recruiter screen", TODAY), "Questions")
    for word in ("motivation", "salary", "notice", "location", "right to work"):
        assert word in rec.lower(), word
    mgr = _section(interviewprep.build_pack(con, uid, "hiring manager", TODAY), "Questions")
    for word in ("scope", "team", "trade-off", "fail"):
        assert word in mgr.lower(), word


def test_a_technical_stage_says_whether_a_coding_bar_is_known():
    con, uid = _setup()
    tech = _section(interviewprep.build_pack(con, uid, "technical", TODAY), "Questions")
    assert "coding round" in tech.lower() and "posting mentions" in tech.lower()
    con2, uid2 = _setup(jd="You will lead the platform group.")
    tech2 = _section(interviewprep.build_pack(con2, uid2, "technical", TODAY), "Questions")
    assert "no coding bar is stated in the posting" in tech2


def test_an_unrecognised_stage_is_said_so_and_gets_general_questions():
    con, uid = _setup()
    s = _section(interviewprep.build_pack(con, uid, "lunch with the CTO", TODAY), "Questions")
    assert "stage not recognised" in s.lower()


def test_the_timeline_lists_events_in_date_order_with_their_sources():
    con, uid = _setup()
    store.add_event(con, uid, "acknowledged", at="2026-09-28", source="mail:abc")
    store.add_event(con, uid, "interviewing", at="2026-10-07", source="mail:def")
    t = _section(interviewprep.build_pack(con, uid, "technical", TODAY), "Timeline")
    assert t.index("2026-09-27") < t.index("2026-09-28") < t.index("2026-10-07")
    assert "mail:abc" in t and "mail:def" in t


def test_evidence_on_file_is_listed_and_old_evidence_is_marked_stale():
    con, uid = _setup()
    store.add_evidence(con, "Acme", source="Glassdoor", figures="GBP 120-150k", fetched_on="2026-10-01")
    store.add_evidence(con, "Acme", source="Levels", figures="GBP 100-130k", fetched_on="2026-04-01")
    e = _section(interviewprep.build_pack(con, uid, "recruiter screen", TODAY), "Evidence on file")
    assert "Glassdoor" in e and "fresh (7 days)" in e
    assert "Levels" in e and "STALE" in e


def test_no_evidence_is_stated_with_the_command_that_adds_it():
    con, uid = _setup()
    e = _section(interviewprep.build_pack(con, uid, "recruiter screen", TODAY), "Evidence on file")
    assert "no salary evidence on file" in e and "job-radar evidence add" in e


def test_the_pack_is_deterministic_and_the_module_has_no_network_or_model_path():
    con, uid = _setup()
    assert (interviewprep.build_pack(con, uid, "technical", TODAY)
            == interviewprep.build_pack(con, uid, "technical", TODAY))
    src = Path(interviewprep.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert not imported & {"subprocess", "urllib", "requests", "socket", "http", "smtplib"}, imported


# --------------------------------------------------------------------- CLI

def _cli():
    d = Path(tempfile.mkdtemp())
    cfg = d / "config.yaml"
    cfg.write_text("titles:\n  include: ['engineering manager']\nlocations:\n  countries: ['UK']\n"
                   "sources:\n  use_bundled: false\n", encoding="utf-8")
    db = d / "t.db"
    con = store.connect(db)
    j = Job(company="Acme", title="Head of Platform", url="https://x.example/1", platform="custom",
            location="London")
    store.upsert_roles(con, [j], run=1)
    store.add_artifact(con, j.uid, "jd_snapshot", body=JD)
    store.add_artifact(con, j.uid, "cv", path="/docs/CV.docx", body=CV)
    store.set_status(con, j.uid, "interviewing")
    con.close()
    return d, cfg, db, j.uid


def _run(cfg, *argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main(["-c", str(cfg), "interview", *argv])
    return code, buf.getvalue()


def test_cli_writes_the_pack_into_the_role_folder_and_prints_the_absolute_path():
    d, cfg, db, uid = _cli()
    docs = d / "docs"
    code, out = _run(cfg, uid, "--stage", "hiring manager", "--docs", str(docs), "--db", str(db))
    assert code == 0, out
    (path,) = list(docs.glob("*/interview-hiring-manager.md"))
    assert str(path.resolve()) in out and path.is_absolute()
    con = store.connect(db)
    row = con.execute("SELECT uid, company, title FROM roles WHERE uid=?", (uid,)).fetchone()
    assert path.parent == runner.role_dir(row, docs)
    assert "## What they will have read" in path.read_text(encoding="utf-8")


def test_cli_does_not_overwrite_unless_forced():
    d, cfg, db, uid = _cli()
    docs = d / "docs"
    args = (uid, "--stage", "technical", "--docs", str(docs), "--db", str(db))
    _run(cfg, *args)
    (path,) = list(docs.glob("*/interview-technical.md"))
    path.write_text("my own notes\n", encoding="utf-8")
    code, out = _run(cfg, *args)
    assert code == 0 and "exists, not overwritten" in out and str(path.resolve()) in out, out
    assert path.read_text(encoding="utf-8") == "my own notes\n"
    code, out = _run(cfg, *args, "--force")
    assert "my own notes" not in path.read_text(encoding="utf-8")


def test_cli_an_unknown_role_is_exit_1():
    d, cfg, db, uid = _cli()
    code, out = _run(cfg, "nothing-like-it", "--stage", "technical", "--docs", str(d / "x"), "--db", str(db))
    assert code == 1 and "Could not identify a role" in out, out


def test_the_application_is_one_line_in_the_timeline_not_two():
    con, uid = _setup()
    t = _section(interviewprep.build_pack(con, uid, "technical", TODAY), "Timeline")
    assert t.count("2026-09-27") == 1, t
    # With no event behind the record, the record still appears.
    con.execute("DELETE FROM app_events WHERE uid=?", (uid,))
    t = _section(interviewprep.build_pack(con, uid, "technical", TODAY), "Timeline")
    assert t.count("2026-09-27") == 1, t
