"""Setup offers to copy the shipped skills where Claude Code finds them.

Cloning the repo leaves `mail-sync` and `interview-prep` inside the checkout,
where Claude Code in an ordinary folder never looks. The install has to be
offered, never forced, and it has to say so when it could not do it.
"""

import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import cli, setup_wizard, skills_install      # noqa: E402


def _tmp():
    return Path(tempfile.mkdtemp())


def _fake_src(*names):
    src = _tmp()
    for n in names:
        (src / n).mkdir()
        (src / n / "SKILL.md").write_text(f"---\nname: {n}\ndescription: x\n---\nbody\n", encoding="utf-8")
    return src


def test_every_skill_the_repo_ships_is_found():
    got = skills_install.available()
    assert {"mail-sync", "interview-prep", "screen-role", "rate-cv", "job-radar-setup"} <= set(got), got


def test_missing_skills_are_copied_whole_and_bytecode_is_left_behind():
    src, dest = _fake_src("alpha"), _tmp()
    (src / "alpha" / "scripts").mkdir()
    (src / "alpha" / "scripts" / "run.py").write_text("print(1)\n", encoding="utf-8")
    (src / "alpha" / "scripts" / "__pycache__").mkdir()
    (src / "alpha" / "scripts" / "__pycache__" / "run.cpython-313.pyc").write_bytes(b"x")
    res = skills_install.install_skills(dest=dest, src=src)
    assert [(r.name, r.state) for r in res] == [("alpha", "installed")]
    assert (dest / "alpha" / "SKILL.md").is_file() and (dest / "alpha" / "scripts" / "run.py").is_file()
    assert not (dest / "alpha" / "scripts" / "__pycache__").exists()
    assert not list(dest.glob(".*.installing")), "a temporary copy was left behind"


def test_an_identical_skill_is_up_to_date_and_untouched():
    src, dest = _fake_src("alpha"), _tmp()
    skills_install.install_skills(dest=dest, src=src)
    before = (dest / "alpha" / "SKILL.md").stat().st_mtime_ns
    res = skills_install.install_skills(dest=dest, src=src)
    assert res[0].state == "up to date"
    assert (dest / "alpha" / "SKILL.md").stat().st_mtime_ns == before


def test_a_skill_the_user_has_edited_is_never_overwritten():
    src, dest = _fake_src("alpha"), _tmp()
    (dest / "alpha").mkdir()
    (dest / "alpha" / "SKILL.md").write_text("my own version\n", encoding="utf-8")
    res = skills_install.install_skills(dest=dest, src=src)
    assert res[0].state == "differs" and "left alone" in res[0].detail
    assert (dest / "alpha" / "SKILL.md").read_text(encoding="utf-8") == "my own version\n"


def test_a_destination_that_cannot_be_made_is_a_failure_not_an_install():
    src, base = _fake_src("alpha"), _tmp()
    blocker = base / "file"
    blocker.write_text("not a folder", encoding="utf-8")       # a file where a folder must go
    res = skills_install.install_skills(dest=blocker / "skills", src=src)
    assert res[0].state == "failed", res
    assert skills_install.report(res, say=lambda *_: None) == 1


def test_no_skills_to_install_is_a_failure_not_an_empty_success():
    res = skills_install.install_skills(dest=_tmp(), src=_tmp())
    assert res[0].state == "failed" and "no skills found" in res[0].detail


def test_the_report_is_zero_only_when_nothing_failed():
    ok = [skills_install.Result("a", "installed"), skills_install.Result("b", "differs", "x")]
    assert skills_install.report(ok, say=lambda *_: None) == 0


# ------------------------------------------------------------- the wiring

def test_the_wizard_offers_the_install_and_a_no_installs_nothing():
    dest, asked = _tmp(), []

    def no(prompt, default=True):
        asked.append(prompt)
        return False
    setup_wizard.offer_skills(ask_yn=no, dest=dest, say=lambda *_: None)
    assert len(asked) == 1 and "skills" in asked[0].lower() and "overwritten" in asked[0]
    assert list(dest.iterdir()) == []


def test_the_wizard_installs_when_the_answer_is_yes():
    dest = _tmp()
    setup_wizard.offer_skills(ask_yn=lambda p, d=True: True, dest=dest, say=lambda *_: None)
    assert (dest / "mail-sync" / "SKILL.md").is_file() and (dest / "interview-prep" / "SKILL.md").is_file()


def _wizard(**kw):
    home = _tmp()
    cv = home / "cv.md"
    cv.write_text("Engineering manager.\n", encoding="utf-8")
    os.environ["CLAUDE_SKILLS_DIR"] = str(home / "skills")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = setup_wizard.run(home / "config.yaml", non_interactive=True, cv=str(cv),
                                    titles="engineering manager", seed=False, **kw)
    finally:
        del os.environ["CLAUDE_SKILLS_DIR"]
    return code, home / "skills", buf.getvalue()


def test_scripted_setup_installs_nothing_unless_asked_to():
    code, skills, out = _wizard()
    assert code == 0 and not skills.exists(), out


def test_scripted_setup_installs_when_the_flag_is_given():
    code, skills, out = _wizard(install_skills=True)
    assert code == 0 and (skills / "mail-sync" / "SKILL.md").is_file(), out
    assert "installed" in out


def test_scripted_setup_exits_nonzero_when_the_install_it_was_asked_for_failed():
    home = _tmp()
    blocker = home / "blocked"
    blocker.write_text("x", encoding="utf-8")
    cv = home / "cv.md"
    cv.write_text("EM\n", encoding="utf-8")
    os.environ["CLAUDE_SKILLS_DIR"] = str(blocker / "skills")
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            code = setup_wizard.run(home / "config.yaml", non_interactive=True, cv=str(cv),
                                    titles="em", seed=False, install_skills=True)
    finally:
        del os.environ["CLAUDE_SKILLS_DIR"]
    assert code == 1


def test_the_install_skills_flag_reaches_the_wizard_only_when_given():
    seen = {}

    def fake(path, non_interactive=False, cv=None, titles=None, scan=False,
             countries=None, currency=None, seed=True, install_skills=None):
        seen["install_skills"] = install_skills
        return 0
    real = setup_wizard.run
    setup_wizard.run = fake
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["setup", "--defaults", "--cv", "x", "--titles", "y"])
            assert seen["install_skills"] is None
            cli.main(["setup", "--defaults", "--cv", "x", "--titles", "y", "--install-skills"])
            assert seen["install_skills"] is True
    finally:
        setup_wizard.run = real


def test_the_install_skills_command_copies_to_the_folder_named():
    dest = _tmp()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main(["install-skills", "--dest", str(dest)])
    assert code == 0 and (dest / "mail-sync" / "SKILL.md").is_file(), buf.getvalue()
    assert "installed" in buf.getvalue()


# ------------------------------------------------- natural-writing (separate repo)

class _Proc:
    def __init__(self, code=0, err=""):
        self.returncode, self.stderr, self.stdout = code, err, ""


def _git_that_clones(cmds):
    """A stand-in for subprocess.run that records the call and makes the folder."""
    def run(cmd, **kw):
        cmds.append(cmd)
        target = Path(cmd[-1])
        (target / ".git").mkdir(parents=True)
        (target / "SKILL.md").write_text("---\nname: natural-writing\ndescription: x\n---\n", encoding="utf-8")
        return _Proc(0)
    return run


def test_natural_writing_is_cloned_when_missing_and_nothing_is_overwritten():
    dest, cmds = _tmp(), []
    r = skills_install.fetch_natural_writing(dest=dest, run=_git_that_clones(cmds), which=lambda n: "/usr/bin/git")
    assert r.state == "installed" and (dest / "natural-writing" / "SKILL.md").is_file()
    assert cmds[0][:4] == ["git", "clone", "--depth", "1"] and skills_install.NATURAL_WRITING_URL in cmds[0]
    assert not list(dest.glob(".*.installing")), "a temporary clone was left behind"
    again = skills_install.fetch_natural_writing(dest=dest, run=_git_that_clones([]), which=lambda n: "/usr/bin/git")
    assert again.state == "up to date"


def test_an_existing_natural_writing_folder_is_never_replaced():
    dest = _tmp()
    (dest / "natural-writing").mkdir()
    (dest / "natural-writing" / "notes.txt").write_text("mine", encoding="utf-8")
    r = skills_install.fetch_natural_writing(dest=dest, run=_git_that_clones([]), which=lambda n: "/usr/bin/git")
    assert r.state == "differs" and (dest / "natural-writing" / "notes.txt").is_file()


def test_no_git_is_a_failure_that_says_the_command_to_run_by_hand():
    r = skills_install.fetch_natural_writing(dest=_tmp(), run=_git_that_clones([]), which=lambda n: None)
    assert r.state == "failed" and "git clone" in r.detail and skills_install.NATURAL_WRITING_URL in r.detail


def test_a_failed_clone_is_a_failure_with_the_reason_and_leaves_nothing_behind():
    dest = _tmp()

    def run(cmd, **kw):
        return _Proc(128, "fatal: unable to access: Could not resolve host")
    r = skills_install.fetch_natural_writing(dest=dest, run=run, which=lambda n: "/usr/bin/git")
    assert r.state == "failed" and "Could not resolve host" in r.detail
    assert not (dest / "natural-writing").exists() and not list(dest.glob(".*.installing"))


def test_the_wizard_asks_a_second_question_about_natural_writing_only_when_it_is_missing():
    dest, asked = _tmp(), []

    def yes(prompt, default=True):
        asked.append(prompt)
        return False if "natural-writing" in prompt else True
    setup_wizard.offer_skills(ask_yn=yes, dest=dest, say=lambda *_: None)
    assert sum("natural-writing" in q for q in asked) == 1, asked
    (dest / "natural-writing").mkdir()
    (dest / "natural-writing" / "SKILL.md").write_text("x", encoding="utf-8")
    asked.clear()
    setup_wizard.offer_skills(ask_yn=yes, dest=dest, say=lambda *_: None)
    assert not any("natural-writing" in q for q in asked), asked


def test_the_install_skills_command_names_natural_writing_when_it_is_missing():
    dest, buf = _tmp(), io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.main(["install-skills", "--dest", str(dest)])
    out = buf.getvalue()
    assert "natural-writing" in out and "--fetch-natural-writing" in out, out
