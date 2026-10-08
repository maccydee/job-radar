"""Copy the skills this repo ships to where Claude Code looks for them.

`generate` reads `skills/` straight out of the checkout, so job-radar itself
needs nothing installed. The skills are also useful on their own, from any
folder, in a normal Claude Code session ("any rejections?", "I have an
interview Tuesday"), and Claude Code only finds those in `~/.claude/skills`.
Cloning the repo does not put them there, and nobody should have to know that.

Rules, each of which is a failure this repo has had in another shape:

- Never overwrite. A skill already there that differs from ours may have been
  edited on purpose; `generate` already prefers the user's copy for the same
  reason. It is reported as `differs` and left alone.
- A copy that could not be made is `failed`, said on screen, and makes the
  command exit non-zero. An unwritable folder must not read as "installed".
- Each skill is copied beside its destination and renamed into place, so an
  interrupted copy never leaves half a skill that looks like a whole one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

NATURAL_WRITING_URL = "https://github.com/maccydee/natural-writing"

INSTALLED = "installed"
UP_TO_DATE = "up to date"
DIFFERS = "differs"
FAILED = "failed"


@dataclass
class Result:
    name: str
    state: str
    detail: str = ""


def bundled_skills_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "skills"


def default_dest() -> Path:
    env = os.environ.get("CLAUDE_SKILLS_DIR", "").strip()
    return Path(env).expanduser() if env else Path.home() / ".claude" / "skills"


def available(src: Path | None = None) -> list[str]:
    """Names of the skills this checkout ships: every folder with a SKILL.md."""
    src = src or bundled_skills_dir()
    if not src.is_dir():
        return []
    return sorted(p.name for p in src.iterdir() if (p / "SKILL.md").is_file())


def _files(root: Path) -> dict[str, bytes]:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
            out[p.relative_to(root).as_posix()] = p.read_bytes()
    return out


def _ignore(_dir, names):
    return [n for n in names if n == "__pycache__" or n.endswith(".pyc")]


def install_skills(dest: Path | None = None, src: Path | None = None,
                   names: list[str] | None = None) -> list[Result]:
    src = src or bundled_skills_dir()
    dest = dest or default_dest()
    wanted = names or available(src)
    if not wanted:
        return [Result("(none)", FAILED, f"no skills found under {src}")]
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return [Result(n, FAILED, f"cannot create {dest}: {e}") for n in wanted]
    results = []
    for name in wanted:
        source, target = src / name, dest / name
        try:
            if not (source / "SKILL.md").is_file():
                results.append(Result(name, FAILED, f"{source} has no SKILL.md"))
                continue
            if target.exists():
                same = _files(source) == _files(target)
                results.append(Result(name, UP_TO_DATE if same else DIFFERS,
                                      str(target) if same else
                                      f"{target} differs from the one shipped here, so it was left alone"))
                continue
            tmp = dest / f".{name}.installing"
            if tmp.exists():
                shutil.rmtree(tmp)
            shutil.copytree(source, tmp, ignore=_ignore)
            os.replace(tmp, target)
            results.append(Result(name, INSTALLED, str(target)))
        except OSError as e:
            results.append(Result(name, FAILED, f"{type(e).__name__}: {e}"))
    return results


def report(results: list[Result], say=print) -> int:
    """Print one line per skill. Returns 1 if any copy failed, else 0."""
    for r in results:
        say(f"  {r.state:<10} {r.name}" + (f"  ({r.detail})" if r.detail and r.state != INSTALLED else ""))
    return 1 if any(r.state == FAILED for r in results) else 0


def natural_writing_present(dest: Path | None = None) -> bool:
    """Whether a usable natural-writing skill is where Claude Code looks."""
    return ((dest or default_dest()) / "natural-writing" / "SKILL.md").is_file()


def fetch_natural_writing(dest: Path | None = None, run=subprocess.run,
                          which=shutil.which) -> Result:
    """Clone natural-writing beside the other skills.

    Not shipped in this repo: it is a general writing skill with a life of its
    own, and the `cv` and `cover_letter` drafting jobs and two of their four
    quality gates need it. This is the one step here that downloads something,
    so it is asked about separately and never runs by default. A folder already
    there is left alone, whatever is in it.
    """
    dest = dest or default_dest()
    target = dest / "natural-writing"
    by_hand = f"git clone {NATURAL_WRITING_URL} {target}"
    if target.exists():
        ok = (target / "SKILL.md").is_file()
        return Result("natural-writing", UP_TO_DATE if ok else DIFFERS,
                      str(target) if ok else f"{target} exists but has no SKILL.md, so it was left alone")
    if not which("git"):
        return Result("natural-writing", FAILED, f"git was not found. By hand: {by_hand}")
    tmp = dest / ".natural-writing.installing"
    try:
        dest.mkdir(parents=True, exist_ok=True)
        if tmp.exists():
            shutil.rmtree(tmp)
        proc = run(["git", "clone", "--depth", "1", NATURAL_WRITING_URL, str(tmp)],
                   capture_output=True, text=True, encoding="utf-8",
                   stdin=subprocess.DEVNULL, timeout=120)
        if proc.returncode != 0:
            return Result("natural-writing", FAILED,
                          (proc.stderr or "git clone failed").strip()[-300:] + f"  By hand: {by_hand}")
        if not (tmp / "SKILL.md").is_file():
            return Result("natural-writing", FAILED, f"the clone has no SKILL.md. By hand: {by_hand}")
        shutil.rmtree(tmp / ".git", ignore_errors=True)
        os.replace(tmp, target)
        return Result("natural-writing", INSTALLED, str(target))
    except (OSError, subprocess.SubprocessError) as e:
        return Result("natural-writing", FAILED, f"{type(e).__name__}: {e}. By hand: {by_hand}")
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
